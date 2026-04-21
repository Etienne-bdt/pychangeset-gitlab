#!/usr/bin/env python3
"""
Changelog generation integrated with version bumping.
Generates changelogs with MR metadata and creates MR descriptions.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import click
import gitlab

# Import from changeset.py to reuse logic
from changeset.changeset import (
    bump_version,
    determine_version_bump,
    find_project_pyproject,
    get_current_version,
)
from changeset.changeset import (
    get_changesets as get_changesets_from_changeset,
)

CHANGESET_DIR = Path(".changeset")
CONFIG_FILE = CHANGESET_DIR / "config.json"


def load_config() -> dict:
    """Load changeset configuration."""
    if not CONFIG_FILE.exists():
        click.echo(click.style("❌ No changeset config found.", fg="red"))
        raise SystemExit(1)

    with open(CONFIG_FILE) as f:
        return json.load(f)


def get_git_info() -> dict:
    """Get git information for the current commit/MR."""
    info = {}

    # Get the current commit hash
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        info["commit"] = result.stdout.strip()[:7]  # Short hash
    except Exception:
        info["commit"] = None

    # Use GitLab CI environment variables when available (pipeline runs).
    # CI_PROJECT_URL already contains the full project URL including the host,
    # so we can skip remote parsing entirely in that case.
    ci_project_url = os.environ.get("CI_PROJECT_URL")
    ci_server_url = os.environ.get("CI_SERVER_URL")
    ci_project_path = os.environ.get("CI_PROJECT_PATH")
    if ci_project_url and ci_server_url and ci_project_path:
        info["repo_url"] = ci_project_url
        info["gitlab_url"] = ci_server_url
        info["project_path"] = ci_project_path
        return info

    # Fall back to parsing the git remote URL (local / user-launched runs).
    # Supports both self-hosted instances and gitlab.com.
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
        remote_url = result.stdout.strip()
        # HTTPS: https://gitlab.example.com/namespace/subgroup/project.git
        https_match = re.match(r"https?://([^/]+)/(.+?)(?:\.git)?$", remote_url)
        # SSH:   git@gitlab.example.com:namespace/subgroup/project.git
        ssh_match = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", remote_url)
        m = https_match or ssh_match
        if m:
            host = m.group(1)
            path = m.group(2)
            info["gitlab_url"] = f"https://{host}"
            info["project_path"] = path
            info["repo_url"] = f"https://{host}/{path}"
    except Exception:
        pass

    return info


def _get_gitlab_client(gitlab_url: str) -> "gitlab.Gitlab | None":
    """Create an authenticated GitLab client.

    Supports both a personal/project access token (GITLAB_TOKEN / PRIVATE_TOKEN)
    and a CI job token (CI_JOB_TOKEN), so the same code works for local runs and
    GitLab CI pipelines on any self-hosted or gitlab.com instance.
    Returns None when no credentials are available.
    """
    private_token = os.environ.get("GITLAB_TOKEN") or os.environ.get("PRIVATE_TOKEN")
    job_token = os.environ.get("CI_JOB_TOKEN")
    try:
        if private_token:
            gl = gitlab.Gitlab(gitlab_url, private_token=private_token)
        elif job_token:
            gl = gitlab.Gitlab(gitlab_url, job_token=job_token)
        else:
            return None
        gl.auth()
        return gl
    except Exception:
        return None


def get_pr_metadata() -> dict:
    """Get MR metadata from GitLab CI environment or git."""
    metadata = {
        # GitLab CI exposes CI_MERGE_REQUEST_IID; fallback to MR_NUMBER for
        # user-launched scripts
        "pr_number": (
            os.environ.get("CI_MERGE_REQUEST_IID") or os.environ.get("MR_NUMBER")
        ),
        # GitLab CI exposes GITLAB_USER_LOGIN; fallback to MR_AUTHOR for
        # user-launched scripts
        "pr_author": (
            os.environ.get("GITLAB_USER_LOGIN") or os.environ.get("MR_AUTHOR")
        ),
        "commit_hash": (
            os.environ.get("CI_COMMIT_SHA") or os.environ.get("COMMIT_SHA", "")
        ),
    }

    if metadata["pr_author"]:
        metadata["pr_author_is_username"] = True

    # Always get git info for repo URL
    git_info = get_git_info()

    # Use git commit if not in environment
    if not metadata["commit_hash"]:
        metadata["commit_hash"] = git_info.get("commit", "")

    # Always use repo URL from git
    metadata["repo_url"] = git_info.get("repo_url", "")

    return metadata


def _username_from_noreply_email(email: str) -> str | None:
    """Extract a GitLab username from a GitLab no-reply email.

    GitLab generates addresses in one of these forms when a user keeps their
    email private:
      • ID+username@users.noreply.gitlab.com          (gitlab.com)
      • ID+username@users.noreply.yourdomain.com      (self-hosted)

    Returns the username string, or None if the email doesn't match.
    """
    m = re.match(r"^\d+\+(.+)@users\.noreply\.", email)
    return m.group(1) if m else None


def get_changeset_metadata(changeset_path: Path) -> dict:
    """Get MR metadata for a specific changeset file.

    Finds the commit that introduced the changeset and extracts metadata
    using the GitLab API (python-gitlab).  Works on self-hosted instances
    and gitlab.com; works both in CI pipelines and local user runs.

    Username resolution order (most → least reliable):
    1. GitLab API via MR object (token required)
    2. GitLab API via commit → MR lookup (token required, handles rebase merges)
    3. GitLab no-reply email parsing (no token required)
    4. GitLab API user search by email (token required, email must be public)
    5. Git author display name (no @ prefix added)
    """
    metadata = {}
    git_info = get_git_info()
    metadata["repo_url"] = git_info.get("repo_url", "")

    gitlab_url = git_info.get("gitlab_url", "https://gitlab.com")
    project_path = git_info.get("project_path", "")

    try:
        # Find the commit that introduced this changeset file
        result = subprocess.run(
            ["git", "log", "--format=%H", "--diff-filter=A", "--", str(changeset_path)],
            capture_output=True,
            text=True,
            check=True,
        )

        if result.stdout.strip():
            commit_hash = result.stdout.strip().split("\n")[0]
            metadata["commit_hash"] = commit_hash

            # Get author name + email from the intro commit for later fallbacks
            author_info_result = subprocess.run(
                ["git", "log", "-1", "--format=%an%x00%ae", commit_hash],
                capture_output=True,
                text=True,
            )
            raw = author_info_result.stdout.strip().split("\x00")
            intro_author_name = raw[0] if raw else ""
            intro_author_email = raw[1] if len(raw) > 1 else ""

            # Try to get a GitLab username from a no-reply email — works without
            # any API token and handles the git-config-name ≠ username case.
            noreply_username = _username_from_noreply_email(intro_author_email)

            # Get the feature-branch commit message (squash-merge = this IS the
            # merge commit, so !123 may live here).
            msg_result = subprocess.run(
                ["git", "log", "-1", "--format=%B", commit_hash],
                capture_output=True,
                text=True,
                check=True,
            )
            commit_msg = msg_result.stdout.strip()

            # GitLab writes "See merge request namespace/project!123" in the
            # true-merge commit, which is separate from the feature-branch commit.
            # Walk ancestry to find the nearest merge commit.
            merge_commit_msg = ""
            try:
                mc_result = subprocess.run(
                    [
                        "git", "log", "--format=%H", "--merges",
                        "--ancestry-path", f"{commit_hash}..HEAD",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                if mc_result.stdout.strip():
                    # Output is newest→oldest; the last entry is the direct merge.
                    direct_merge_hash = mc_result.stdout.strip().split("\n")[-1]
                    mc_msg_result = subprocess.run(
                        ["git", "log", "-1", "--format=%B", direct_merge_hash],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    merge_commit_msg = mc_msg_result.stdout.strip()
            except Exception:
                pass

            # Search for MR IID in merge-commit first, then intro commit.
            mr_match = re.search(r"!(\d+)", merge_commit_msg) or re.search(
                r"!(\d+)", commit_msg
            )
            # Combined text for Co-authored-by trailer scanning
            all_commit_text = commit_msg + "\n" + merge_commit_msg

            gl = _get_gitlab_client(gitlab_url)

            # ── Step 1: we have an MR IID from git history ──────────────────
            if mr_match:
                mr_iid = mr_match.group(1)
                metadata["pr_number"] = mr_iid

                if gl and project_path:
                    try:
                        project = gl.projects.get(project_path)
                        mr = project.mergerequests.get(int(mr_iid))

                        metadata["pr_author"] = mr.author["username"]
                        metadata["pr_author_is_username"] = True
                        # Record the git-level name/email we know belong to this
                        # author so Co-authored-by trailers can be deduplicated
                        # even when the git config name differs from the GitLab
                        # display name.
                        metadata["pr_author_git_name"] = intro_author_name
                        metadata["pr_author_git_email"] = intro_author_email
                        print(
                            f"✓ Got GitLab username for MR !{mr_iid}: "
                            f"{metadata['pr_author']}"
                        )

                        # Build pr_author_info for co-author deduplication
                        pr_author_info: dict = {}
                        try:
                            users = gl.users.list(username=metadata["pr_author"])
                            if users:
                                user = users[0]
                                pr_author_info = {
                                    "login": metadata["pr_author"],
                                    "name": getattr(user, "name", ""),
                                    "email": (
                                        getattr(user, "public_email", "")
                                        or getattr(user, "email", "")
                                    ),
                                }
                                metadata["pr_author_info"] = pr_author_info
                        except Exception:
                            pass

                        # Collect co-authors from MR commits
                        try:
                            pr_author = metadata["pr_author"]
                            gitlab_users: dict = {}

                            for c in mr.commits():
                                c_author_name = getattr(c, "author_name", "")
                                c_author_email = getattr(c, "author_email", "")
                                if not c_author_name:
                                    continue

                                username = (
                                    _username_from_noreply_email(c_author_email)
                                )
                                if not username:
                                    try:
                                        found = gl.users.list(search=c_author_email)
                                        for u in found:
                                            u_email = (
                                                getattr(u, "public_email", "")
                                                or getattr(u, "email", "")
                                            )
                                            if u_email == c_author_email:
                                                username = u.username
                                                break
                                    except Exception:
                                        pass

                                key = username or c_author_name
                                # Skip if this is the PR author — compare by
                                # resolved username, git name, and git email so
                                # a mismatch between git config name and GitLab
                                # username doesn't cause a false co-author.
                                is_author = (
                                    key == pr_author
                                    or c_author_name == intro_author_name
                                    or (
                                        intro_author_email
                                        and c_author_email == intro_author_email
                                    )
                                )
                                if not is_author:
                                    gitlab_users[key] = {
                                        "login": username,
                                        "name": c_author_name,
                                        "email": c_author_email,
                                    }

                            if gitlab_users:
                                metadata["co_authors"] = [
                                    (key, info["login"] is not None)
                                    for key, info in gitlab_users.items()
                                ]
                                metadata["github_user_info"] = gitlab_users
                        except Exception:
                            pass

                    except Exception as e:
                        print(f"⚠️  GitLab API failed for MR !{mr_iid}: {e!s}")

            # ── Step 2: no !IID in git history — try API commit→MR lookup ───
            # This handles rebase / fast-forward merges where no merge commit
            # exists in the local git graph.
            if not metadata.get("pr_number") and gl and project_path:
                try:
                    project = gl.projects.get(project_path)
                    associated_mrs = project.commits.get(commit_hash).merge_requests()
                    if associated_mrs:
                        mr = associated_mrs[0]
                        mr_iid = str(mr.iid)
                        metadata["pr_number"] = mr_iid
                        mr_obj = project.mergerequests.get(mr.iid)
                        metadata["pr_author"] = mr_obj.author["username"]
                        metadata["pr_author_is_username"] = True
                        metadata["pr_author_git_name"] = intro_author_name
                        metadata["pr_author_git_email"] = intro_author_email
                        print(
                            f"✓ Got MR !{mr_iid} via commit API for "
                            f"{commit_hash[:7]}: {metadata['pr_author']}"
                        )
                except Exception:
                    pass

            # ── Step 3: resolve author username if still unknown ─────────────
            if not metadata.get("pr_author"):
                # 3a. No-reply email — no token needed
                if noreply_username:
                    metadata["pr_author"] = noreply_username
                    metadata["pr_author_is_username"] = True
                    print(
                        f"✓ Resolved username from no-reply email: "
                        f"{noreply_username}"
                    )
                # 3b. Email search — requires an authenticated client.
                elif gl and intro_author_email:
                    try:
                        found = gl.users.list(search=intro_author_email)
                        for u in found:
                            u_email = (
                                getattr(u, "public_email", "")
                                or getattr(u, "email", "")
                            )
                            if u_email == intro_author_email:
                                metadata["pr_author"] = u.username
                                metadata["pr_author_is_username"] = True
                                metadata["pr_author_git_name"] = intro_author_name
                                metadata["pr_author_git_email"] = intro_author_email
                                print(
                                    f"✓ Resolved username by email: "
                                    f"{u.username}"
                                )
                                break
                    except Exception:
                        pass

                # 3c. Display-name search — requires an authenticated client;
                # only applied when exactly one user matches (avoids false positives).
                if not metadata.get("pr_author") and gl and intro_author_name:
                    try:
                        found = gl.users.list(search=intro_author_name)
                        if len(found) == 1:
                            metadata["pr_author"] = found[0].username
                            metadata["pr_author_is_username"] = True
                            metadata["pr_author_git_name"] = intro_author_name
                            metadata["pr_author_git_email"] = intro_author_email
                            print(
                                f"✓ Resolved username by display name "
                                f"'{intro_author_name}': {found[0].username}"
                            )
                    except Exception:
                        pass

                # 3d. Final fallback: git display name, no @ prefix
                if not metadata.get("pr_author") and intro_author_name:
                    metadata["pr_author"] = intro_author_name
                    metadata["pr_author_is_username"] = False
                    print(
                        f"⚠️  Using git author name (no @ will be added): "
                        f"{intro_author_name}"
                    )

            # ── Co-authored-by trailers ───────────────────────────────────────
            co_authors_from_commits = []
            pr_author_info = metadata.get("pr_author_info", {})
            # Git-level aliases for the PR author (git config name/email may
            # differ from the GitLab display name, so check both).
            pr_author_git_name = metadata.get("pr_author_git_name", "")
            pr_author_git_email = metadata.get("pr_author_git_email", "")

            for line in all_commit_text.split("\n"):
                co_author_match = re.match(
                    r"^Co-authored-by:\s*(.+?)\s*<(.+?)>$", line.strip()
                )
                if co_author_match:
                    co_author_name = co_author_match.group(1).strip()
                    co_author_email = co_author_match.group(2).strip()

                    is_pr_author = (
                        co_author_name == metadata.get("pr_author")
                        or (pr_author_git_email
                            and co_author_email == pr_author_git_email)
                        or (pr_author_git_name
                            and co_author_name == pr_author_git_name)
                        or (pr_author_info
                            and co_author_email == pr_author_info.get("email", ""))
                        or (pr_author_info
                            and co_author_name == pr_author_info.get("name", ""))
                    )

                    if co_author_name and not is_pr_author:
                        co_authors_from_commits.append(
                            {"name": co_author_name, "email": co_author_email}
                        )

            if "co_authors" in metadata and metadata.get("github_user_info"):
                gitlab_users = metadata.get("github_user_info", {})
                final_co_authors = list(metadata["co_authors"])

                for commit_author in co_authors_from_commits:
                    is_duplicate = any(
                        commit_author["email"] == ui.get("email", "")
                        or commit_author["name"] == ui.get("name", "")
                        for ui in gitlab_users.values()
                    )
                    if not is_duplicate:
                        final_co_authors.append((commit_author["name"], False))

                metadata["co_authors"] = final_co_authors
            elif co_authors_from_commits:
                metadata["co_authors"] = [
                    (
                        _username_from_noreply_email(a["email"]) or a["name"],
                        _username_from_noreply_email(a["email"]) is not None,
                    )
                    for a in co_authors_from_commits
                ]

    except subprocess.CalledProcessError:
        pass

    # Fall back to GitLab CI / user-supplied environment variables
    if not metadata.get("pr_number"):
        metadata["pr_number"] = (
            os.environ.get("CI_MERGE_REQUEST_IID")
            or os.environ.get("MR_NUMBER", "")
        )
    if not metadata.get("pr_author"):
        metadata["pr_author"] = (
            os.environ.get("GITLAB_USER_LOGIN")
            or os.environ.get("MR_AUTHOR", "")
        )
        if metadata["pr_author"]:
            metadata["pr_author_is_username"] = True
    if not metadata.get("commit_hash"):
        metadata["commit_hash"] = os.environ.get(
            "CI_COMMIT_SHA",
            os.environ.get("COMMIT_SHA", git_info.get("commit", "")),
        )

    return metadata


def format_changelog_entry(entry: dict, config: dict, pr_metadata: dict) -> str:
    """Format a single changelog entry with PR and commit info."""
    description = entry["description"]
    pr_number = pr_metadata.get("pr_number")
    pr_author = pr_metadata.get("pr_author")
    pr_author_is_username = pr_metadata.get("pr_author_is_username", False)
    co_authors = pr_metadata.get("co_authors", [])
    # Support legacy format where co_authors might be simple strings
    if co_authors and isinstance(co_authors[0], str):
        # Convert legacy format to new tuple format
        co_authors_are_usernames = pr_metadata.get("co_authors_are_usernames", False)
        co_authors = [(author, co_authors_are_usernames) for author in co_authors]
    commit_hash = pr_metadata.get("commit_hash", "")[:7]
    repo_url = pr_metadata.get("repo_url", "")

    # Build the entry
    parts = []

    # Add MR link if available
    if pr_number and repo_url:
        parts.append(f"[!{pr_number}]({repo_url}/-/merge_requests/{pr_number})")

    # Add commit link if available
    if commit_hash and repo_url:
        parts.append(f"[`{commit_hash}`]({repo_url}/-/commit/{commit_hash})")

    # Add author thanks if available
    authors_to_thank = []
    if pr_author:
        # Only add @ if we have a GitHub username, not a display name
        if pr_author.startswith("@"):
            authors_to_thank.append(pr_author)
        elif pr_author_is_username:
            authors_to_thank.append(f"@{pr_author}")
        else:
            # Display name from git - don't add @
            authors_to_thank.append(pr_author)

    # Add co-authors
    for co_author_entry in co_authors:
        # Handle both new tuple format and legacy string format
        if isinstance(co_author_entry, tuple):
            co_author, is_username = co_author_entry
            if co_author.startswith("@"):
                authors_to_thank.append(co_author)
            elif is_username:
                authors_to_thank.append(f"@{co_author}")
            else:
                # Display name from git - don't add @
                authors_to_thank.append(co_author)
        else:
            # Legacy format - just a string
            if co_author_entry.startswith("@"):
                authors_to_thank.append(co_author_entry)
            else:
                # Assume it's a display name without context
                authors_to_thank.append(co_author_entry)

    if authors_to_thank:
        if len(authors_to_thank) == 1:
            parts.append(f"by : {authors_to_thank[0]}.")
        else:
            # Format multiple authors nicely
            all_but_last = ", ".join(authors_to_thank[:-1])
            parts.append(f"by : {all_but_last} and {authors_to_thank[-1]}.")

    # Add description
    parts.append(f"- {description}")

    return " ".join(parts)


def generate_changelog_section(
    package: str, new_version: str, entries: list[dict], config: dict, pr_metadata: dict
) -> str:
    """Generate changelog section for a package version."""
    lines = []

    # Add version header
    lines.append(f"## {new_version}")
    lines.append("")

    # Group entries by change type
    grouped = {}
    for entry in entries:
        change_type = entry["type"]
        if change_type not in grouped:
            grouped[change_type] = []
        grouped[change_type].append(entry)

    # Add sections for each change type
    for change_type in ["major", "minor", "patch"]:
        if change_type not in grouped:
            continue

        # Get the change type label
        type_label = {
            "major": "Major Changes",
            "minor": "Minor Changes",
            "patch": "Patch Changes",
        }.get(change_type, f"{change_type.capitalize()} Changes")

        lines.append(f"### {type_label}")
        lines.append("")

        # Add each entry
        for entry in grouped[change_type]:
            # Get metadata specific to this changeset if available
            if "filepath" in entry:
                changeset_metadata = get_changeset_metadata(entry["filepath"])
            else:
                changeset_metadata = pr_metadata
            lines.append(format_changelog_entry(entry, config, changeset_metadata))

        lines.append("")

    return "\n".join(lines).strip()


def update_or_create_changelog(
    changelog_path: Path, package_name: str, new_section: str
) -> bool:
    """Update or create a changelog file."""
    if changelog_path.exists():
        content = changelog_path.read_text()
    else:
        # Create new changelog with package name header
        content = f"# {package_name}\n\n"

    # Insert the new section after the package name header
    lines = content.split("\n")
    insert_index = None

    # Find where to insert (after header, before first version)
    for i, line in enumerate(lines):
        if line.startswith("# "):
            # Found header, insert after next blank line
            for j in range(i + 1, len(lines)):
                if not lines[j].strip():
                    insert_index = j + 1
                    break
            if insert_index is None:
                insert_index = i + 1
            break

    if insert_index is None:
        # No header found, just prepend
        new_content = new_section + "\n\n" + content
    else:
        # Insert at the found position
        lines.insert(insert_index, new_section)
        lines.insert(insert_index + 1, "")
        new_content = "\n".join(lines)

    # Write the updated content
    changelog_path.write_text(new_content)
    return True


def generate_pr_description(package_updates: list[dict]) -> str:
    """Generate a combined PR description for all package updates."""
    lines = ["# Releases", ""]

    for update in package_updates:
        package = update["package"]
        version = update["version"]
        changelog_content = update["changelog_content"]

        # Add package header
        lines.append(f"## {package}@{version}")
        lines.append("")

        # Add the changelog content (without the package header)
        # Skip the first line if it's a version header
        changelog_lines = changelog_content.split("\n")
        start_index = 0
        if changelog_lines and changelog_lines[0].startswith("## "):
            start_index = 1

        lines.extend(changelog_lines[start_index:])
        lines.append("")

    return "\n".join(lines)


def process_changesets_for_changelog() -> tuple[list[dict], str]:
    """
    Process changesets to generate changelog entries and PR description.
    Returns (package_updates, pr_description).
    """
    config = load_config()
    pr_metadata = get_pr_metadata()

    # Get all changesets
    changesets = get_changesets_from_changeset()
    if not changesets:
        return [], ""

    # Group changesets by package
    package_changes = {}
    changeset_files = set()

    for filepath, package, change_type, desc in changesets:
        changeset_files.add(filepath)
        if package not in package_changes:
            package_changes[package] = {"changes": [], "descriptions": []}
        package_changes[package]["changes"].append(change_type)
        package_changes[package]["descriptions"].append(
            {
                "type": change_type,
                "description": desc,
                "changeset": filepath.name,
                "filepath": filepath,
            }
        )

    # Process each package
    package_updates = []

    for package, info in package_changes.items():
        # Find pyproject.toml
        try:
            pyproject_path = find_project_pyproject(package)
        except ValueError as e:
            click.echo(click.style(f"⚠️  {e}", fg="yellow"))
            continue

        # Determine new version
        bump_type = determine_version_bump(info["changes"])
        current_version = get_current_version(pyproject_path)
        new_version = bump_version(current_version, bump_type)

        # Generate changelog content
        changelog_content = generate_changelog_section(
            package, new_version, info["descriptions"], config, pr_metadata
        )

        # Find changelog path (same directory as pyproject.toml)
        changelog_path = pyproject_path.parent / "CHANGELOG.md"

        package_updates.append(
            {
                "package": package,
                "version": new_version,
                "current_version": current_version,
                "changelog_path": changelog_path,
                "changelog_content": changelog_content,
                "pyproject_path": pyproject_path,
            }
        )

    # Generate PR description
    pr_description = generate_pr_description(package_updates)

    return package_updates, pr_description


@click.command()
@click.option(
    "--dry-run", is_flag=True, help="Show what would be done without making changes"
)
@click.option("--output-pr-description", help="File to write PR description to")
def main(dry_run: bool, output_pr_description: str):
    """Generate changelogs from changesets with version bumping."""

    click.echo(click.style("📜 Generating changelogs...\n", fg="cyan", bold=True))

    # Process changesets
    package_updates, pr_description = process_changesets_for_changelog()

    if not package_updates:
        click.echo(click.style("No changesets found. Nothing to do!", fg="yellow"))
        return

    # Show what will be done
    click.echo(
        click.style(f"Found updates for {len(package_updates)} package(s):", fg="green")
    )
    for update in package_updates:
        current = update["current_version"]
        new = update["version"]
        click.echo(f"  📦 {update['package']}: {current} → {new}")

    if dry_run:
        click.echo(
            click.style("\n🔍 Dry run mode - no changes will be made", fg="yellow")
        )
        click.echo("\n" + "=" * 60)
        click.echo(click.style("MR Description:", fg="cyan"))
        click.echo("=" * 60)
        click.echo(pr_description)
        click.echo("=" * 60)

        for update in package_updates:
            click.echo(
                click.style(f"\nChangelog for {update['changelog_path']}:", fg="cyan")
            )
            click.echo("-" * 60)
            click.echo(update["changelog_content"])
            click.echo("-" * 60)
        return

    # Update changelog files
    for update in package_updates:
        success = update_or_create_changelog(
            update["changelog_path"], update["package"], update["changelog_content"]
        )

        if success:
            click.echo(
                click.style(f"✅ Updated {update['changelog_path']}", fg="green")
            )
        else:
            click.echo(
                click.style(f"❌ Failed to update {update['changelog_path']}", fg="red")
            )

    # Write PR description if requested
    if output_pr_description:
        Path(output_pr_description).write_text(pr_description)
        click.echo(
            click.style(
                f"✅ Wrote PR description to {output_pr_description}", fg="green"
            )
        )

    click.echo(
        click.style("\n✅ Changelog generation complete!", fg="green", bold=True)
    )


if __name__ == "__main__":
    main()
