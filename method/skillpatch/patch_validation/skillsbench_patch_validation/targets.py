from pathlib import Path, PurePosixPath

def _relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Target must be a canonical relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value or not path.parts:
        raise ValueError("Target must be a canonical relative path")
    return path


def _checked_file(root: Path, relative_path: str) -> Path:
    relative = _relative_path(relative_path)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Skill root must be a real directory")
    path = root
    for component in relative.parts:
        path = path / component
        if path.is_symlink():
            raise ValueError("Skill target paths cannot contain symlinks")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Target is missing or outside the original Skill")
    return path


def locate_skill_root(pack: Path, target_relative_path: str) -> tuple[Path, str]:
    """Find the nearest enclosing SKILL.md without conflating same-named Skills."""
    path = _checked_file(pack, target_relative_path)
    directory = path.parent
    while True:
        declaration = directory / "SKILL.md"
        if declaration.is_symlink():
            raise ValueError("Skill declaration cannot be a symlink")
        if declaration.is_file():
            relative = directory.relative_to(pack).as_posix()
            return directory, "" if relative == "." else relative
        if directory == pack:
            break
        directory = directory.parent
    raise ValueError("Target has no enclosing original Skill declaration")
