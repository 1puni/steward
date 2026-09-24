import subprocess
from pathlib import Path

def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, str, str]:
    bare = tmp_path / "origin.git"
    _git("init", "-q", "-b", "main", "--bare", str(bare), cwd=tmp_path)
    clone = tmp_path / "repo"
    _git("clone", "-q", str(bare), str(clone), cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=clone)
    _git("config", "user.email", "test@example.invalid", cwd=clone)
    (clone / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=clone)
    _git("commit", "-q", "-m", "base", cwd=clone)
    _git("push", "-q", "origin", "main", cwd=clone)
    base = _git("rev-parse", "HEAD", cwd=clone)

    _git("switch", "-q", "-c", "steward", cwd=clone)
    (clone / "ambient.txt").write_text("reconciled\n")
    _git("add", "ambient.txt", cwd=clone)
    _git("commit", "-q", "-m", "ambient work", cwd=clone)
    work = _git("rev-parse", "HEAD", cwd=clone)
    return bare, clone, base, work
