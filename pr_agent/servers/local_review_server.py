"""
Local Review Server - HTTP API for code reviews with Ollama
============================================================

Exposes PR-Agent review capabilities via a simple HTTP API.
Designed to run on mdp-gpu alongside Ollama for zero-latency inference.

Endpoints:
    POST /review     - Review a git diff
    POST /improve    - Get code improvement suggestions
    POST /describe   - Generate PR description
    GET  /health     - Health check
    GET  /models     - List available Ollama models
"""

import asyncio
import tempfile
import os
import shutil
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
import httpx

from pr_agent.config_loader import get_settings
from pr_agent.agent.pr_agent import PRAgent
from pr_agent.git_providers import get_git_provider
from pr_agent.git_providers.local_git_provider import LocalGitProvider


# Configuration
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("PR_AGENT_MODEL", "ollama/qwen3-coder:30b")


class ReviewRequest(BaseModel):
    """Request body for review endpoints."""
    diff: Optional[str] = None  # Raw git diff
    repo_path: Optional[str] = None  # Local repo path (for server-side repos)
    base_branch: Optional[str] = "main"  # Base branch for comparison
    target_branch: Optional[str] = "HEAD"  # Target branch/commit
    model: Optional[str] = None  # Override default model


class ReviewResponse(BaseModel):
    """Response from review endpoints."""
    success: bool
    command: str
    review: Optional[str] = None
    suggestions: Optional[list] = None
    description: Optional[str] = None
    error: Optional[str] = None
    model: str
    tokens_used: Optional[int] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    # Verify Ollama connection on startup
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5.0)
            models = resp.json().get("models", [])
            print(f"✓ Connected to Ollama at {OLLAMA_BASE_URL}")
            print(f"✓ {len(models)} models available")
    except Exception as e:
        print(f"⚠ Warning: Cannot connect to Ollama at {OLLAMA_BASE_URL}: {e}")

    yield

    print("Shutting down Local Review Server")


app = FastAPI(
    title="PR-Agent Local Review Server",
    description="Code review API powered by Ollama",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    ollama_ok = False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5.0)
            ollama_ok = resp.status_code == 200
    except:
        pass

    return {
        "status": "healthy" if ollama_ok else "degraded",
        "ollama": "connected" if ollama_ok else "disconnected",
        "ollama_url": OLLAMA_BASE_URL,
        "default_model": DEFAULT_MODEL
    }


@app.get("/models")
async def list_models():
    """List available Ollama models."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10.0)
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            # Filter to coding-related models
            coding_models = [m for m in models if any(k in m.lower() for k in
                ["coder", "code", "qwen", "llama", "deepseek", "starcoder"])]
            return {
                "total": len(models),
                "coding_models": coding_models,
                "default": DEFAULT_MODEL
            }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Cannot connect to Ollama: {e}")


async def run_pr_agent_command(command: str, diff: str, model: str) -> dict:
    """Run a PR-Agent command on the given diff."""
    # Create a temporary git repo with the diff
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp(prefix="pr-agent-review-")

        # Initialize a git repo
        os.system(f"cd {temp_dir} && git init -q")
        os.system(f"cd {temp_dir} && git config user.email 'review@local'")
        os.system(f"cd {temp_dir} && git config user.name 'Review Bot'")

        # Create initial commit
        Path(temp_dir, "README.md").write_text("# Review\n")
        os.system(f"cd {temp_dir} && git add -A && git commit -q -m 'init'")

        # Apply the diff
        diff_file = Path(temp_dir, "changes.patch")
        diff_file.write_text(diff)

        # Try to apply the patch
        result = os.system(f"cd {temp_dir} && git apply --check {diff_file} 2>/dev/null")
        if result == 0:
            os.system(f"cd {temp_dir} && git apply {diff_file}")
            os.system(f"cd {temp_dir} && git add -A && git commit -q -m 'changes'")

        # Configure PR-Agent settings
        settings = get_settings()
        settings.config.model = model
        settings.config.git_provider = "local"
        settings.config.publish_output = False
        settings.config.verbosity_level = 0

        # Create local git provider
        provider = LocalGitProvider(temp_dir)

        # Run the command
        agent = PRAgent()

        # Capture output
        output_file = Path(temp_dir, f"pr_{command}.md")
        settings.local.review_path = str(output_file) if command == "review" else ""
        settings.local.description_path = str(output_file) if command == "describe" else ""

        await agent.handle_request(temp_dir, f"/{command}")

        # Read output
        review_text = None
        if output_file.exists():
            review_text = output_file.read_text()

        return {
            "success": True,
            "command": command,
            "review": review_text,
            "model": model
        }

    except Exception as e:
        return {
            "success": False,
            "command": command,
            "error": str(e),
            "model": model
        }
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


@app.post("/review", response_model=ReviewResponse)
async def review_diff(request: ReviewRequest = Body(...)):
    """
    Review a git diff.

    Send the output of `git diff` and receive a code review.

    Example:
        git diff main..HEAD | curl -X POST http://localhost:8000/review \\
            -H "Content-Type: application/json" \\
            -d '{"diff": "$(cat)"}'
    """
    if not request.diff:
        raise HTTPException(status_code=400, detail="diff is required")

    model = request.model or DEFAULT_MODEL
    result = await run_pr_agent_command("review", request.diff, model)
    return ReviewResponse(**result)


@app.post("/improve", response_model=ReviewResponse)
async def improve_diff(request: ReviewRequest = Body(...)):
    """
    Get improvement suggestions for a git diff.
    """
    if not request.diff:
        raise HTTPException(status_code=400, detail="diff is required")

    model = request.model or DEFAULT_MODEL
    result = await run_pr_agent_command("improve", request.diff, model)
    return ReviewResponse(**result)


@app.post("/describe", response_model=ReviewResponse)
async def describe_diff(request: ReviewRequest = Body(...)):
    """
    Generate a PR description for a git diff.
    """
    if not request.diff:
        raise HTTPException(status_code=400, detail="diff is required")

    model = request.model or DEFAULT_MODEL
    result = await run_pr_agent_command("describe", request.diff, model)
    return ReviewResponse(**result)


@app.post("/review/raw", response_class=PlainTextResponse)
async def review_diff_raw(
    diff: str = Body(..., media_type="text/plain"),
    model: Optional[str] = Query(None)
):
    """
    Review a raw diff (plain text body).

    Simpler API for piping diffs directly:
        git diff | curl -X POST http://localhost:8000/review/raw -d @-
    """
    use_model = model or DEFAULT_MODEL
    result = await run_pr_agent_command("review", diff, use_model)

    if result.get("success") and result.get("review"):
        return result["review"]
    else:
        return f"Error: {result.get('error', 'Unknown error')}"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
