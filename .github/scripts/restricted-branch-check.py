#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31.0",
#     "GitPython>=3.1.32"
# ]
# [tool.uv]
# exclude-newer = "2025-02-19T00:00:00Z"
# ///

import os
import re
import sys
import json
import shutil
import tempfile
from pathlib import Path
from xml.etree import ElementTree
import requests
import git


def clone_manifest_repo(temp_dir: Path) -> Path:
    """Clone the manifest repository to a temporary directory."""
    manifest_dir = temp_dir / "manifest"
    print("Cloning manifest repository...")
    git.Repo.clone_from("https://github.com/couchbase/manifest.git", manifest_dir)
    return manifest_dir


def find_all_manifests(manifest_dir: Path) -> list[Path]:
    """Find all XML manifest files in the repository."""
    manifest_files = []
    for xml_file in manifest_dir.rglob("*.xml"):
        # Skip files in .git directory and other non-manifest files
        if xml_file.name not in ["pom.xml"]:
            # Skip toy/ and released/ directories
            relative_path = xml_file.relative_to(manifest_dir)
            path_parts = relative_path.parts
            if path_parts[0] not in ["toy", "released"]:
                manifest_files.append(xml_file)
    return manifest_files


def parse_manifest(manifest_path: Path) -> dict:
    """Parse a manifest XML file and extract project information."""
    try:
        tree = ElementTree.parse(manifest_path)
        root = tree.getroot()

        # Get default branch
        default_branch = "master"
        default_elem = root.find("default")
        if default_elem is not None:
            default_branch = default_elem.get("revision", "master")

        # Extract all projects
        projects = []
        for project_elem in root.findall("project"):
            project_name = project_elem.get("name")
            project_revision = project_elem.get("revision", default_branch)
            if project_name:
                projects.append({
                    "name": project_name,
                    "revision": project_revision
                })

        # Also check extend-project elements
        for project_elem in root.findall("extend-project"):
            project_name = project_elem.get("name")
            project_revision = project_elem.get("revision", default_branch)
            if project_name:
                projects.append({
                    "name": project_name,
                    "revision": project_revision
                })

        return {
            "projects": projects,
            "default_branch": default_branch,
            "manifest_tree": root
        }
    except Exception as e:
        print(f"Warning: Could not parse manifest {manifest_path}: {e}")
        return {"projects": [], "default_branch": "master", "manifest_tree": None}


def load_product_configs(manifest_dir: Path) -> dict:
    """Load all product-config.json files and their associated restriction info."""
    product_configs = {}

    for config_file in manifest_dir.rglob("product-config.json"):
        try:
            with open(config_file, 'r') as f:
                config_data = json.load(f)

            # Get the product directory (parent of product-config.json)
            product_dir = config_file.parent
            relative_product_dir = product_dir.relative_to(manifest_dir)

            product_configs[str(relative_product_dir)] = config_data
            print(f"Loaded product config for: {relative_product_dir}")

        except Exception as e:
            print(f"Warning: Could not load product config {config_file}: {e}")

    return product_configs


def check_project_in_manifest(project_name: str, branch_name: str, manifest_data: dict) -> bool:
    """Check if a project/branch combination is referenced in a manifest."""
    for project in manifest_data["projects"]:
        if project["name"] == project_name:
            # If project found, check if branch matches
            if project["revision"] == branch_name:
                return True
            # Also check if the manifest's default branch matches and project uses default
            if project["revision"] == manifest_data["default_branch"] and branch_name == manifest_data["default_branch"]:
                return True
    return False


def get_restricted_manifests(project_name: str, branch_name: str, manifest_dir: Path) -> list[dict]:
    """Find all restricted manifests that reference the given project/branch."""
    restricted_manifests = []

    # Load all product configs
    product_configs = load_product_configs(manifest_dir)

    # Find all manifest files
    manifest_files = find_all_manifests(manifest_dir)

    for manifest_file in manifest_files:
        # Parse the manifest
        manifest_data = parse_manifest(manifest_file)

        # Check if this manifest references our project/branch
        if check_project_in_manifest(project_name, branch_name, manifest_data):
            print(f"Project {project_name} (branch: {branch_name}) found in manifest: {manifest_file}")

            # Now check if this manifest is restricted in any product config
            relative_manifest_path = manifest_file.relative_to(manifest_dir)

            for product_dir, config_data in product_configs.items():
                manifests_config = config_data.get("manifests", {})

                # Check various possible manifest path formats
                manifest_keys_to_check = [
                    str(relative_manifest_path),
                    str(relative_manifest_path).replace("\\", "/"),  # Windows path compatibility
                    manifest_file.name,  # Just filename
                ]

                for manifest_key in manifest_keys_to_check:
                    if manifest_key in manifests_config:
                        manifest_config = manifests_config[manifest_key]
                        if manifest_config.get("restricted", False):
                            approval_ticket = manifest_config.get("approval_ticket")
                            if approval_ticket:
                                restricted_manifests.append({
                                    "manifest_path": str(relative_manifest_path),
                                    "product_dir": product_dir,
                                    "approval_ticket": approval_ticket,
                                    "release_name": manifest_config.get("release_name", manifest_key),
                                    "config": manifest_config
                                })
                                print(f"Found restricted manifest: {manifest_key} (approval ticket: {approval_ticket})")
                        break

    return restricted_manifests


def get_jira_keys_from_commits(repo: str, pr_number: str, gh_token: str) -> set[str]:
    """Extract JIRA issue keys from commit messages in the PR."""
    commits_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/commits"
    headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}

    response = requests.get(commits_url, headers=headers, timeout=10)
    if response.status_code != 200:
        print(f"Error: GitHub API returned {response.status_code} when fetching commits for PR #{pr_number}")
        return set()

    commits = response.json()
    jira_keys = set()
    jira_pattern = re.compile(r'\b[A-Z]{2,}-\d+\b')

    for commit in commits:
        msg = commit.get('commit', {}).get('message', '')
        for key in jira_pattern.findall(msg):
            jira_keys.add(key)

    return jira_keys


def get_approved_jira_keys(approval_ticket: str, jira_url: str, jira_user: str, jira_token: str) -> set[str]:
    """Get all JIRA keys that are approved (linked to or subtasks of the approval ticket)."""
    issue_api = f"{jira_url}/rest/api/2/issue/{approval_ticket}?fields=issuelinks,subtasks"
    resp = requests.get(issue_api, auth=(jira_user, jira_token), timeout=10)

    if resp.status_code != 200:
        print(f"Error: Failed to fetch JIRA issue {approval_ticket} (status {resp.status_code})")
        return set()

    issue_data = resp.json()
    fields = issue_data.get('fields', {})

    approved_keys = set()

    # Add linked issues
    for link in fields.get('issuelinks', []):
        if 'outwardIssue' in link:
            approved_keys.add(link['outwardIssue'].get('key'))
        if 'inwardIssue' in link:
            approved_keys.add(link['inwardIssue'].get('key'))

    # Add subtasks
    for subtask in fields.get('subtasks', []):
        approved_keys.add(subtask.get('key'))

    # Add the approval ticket itself
    approved_keys.add(approval_ticket)

    # Remove None entries
    approved_keys.discard(None)

    return approved_keys


def main():
    # Get environment variables
    base_branch = os.getenv('GITHUB_BASE_REF')
    repo = os.getenv('REPO')
    pr_number = os.getenv('PR_NUMBER')
    gh_token = os.getenv('GITHUB_TOKEN')

    jira_url = os.getenv('JIRA_URL', '').rstrip('/')
    jira_user = os.getenv('JIRA_USERNAME')
    jira_token = os.getenv('JIRA_API_TOKEN')

    if not base_branch:
        print("Error: GITHUB_BASE_REF is not set. This action runs only on pull_request events.")
        sys.exit(1)

    if not all([repo, pr_number, gh_token]):
        print("Error: Missing GitHub context (REPO/PR_NUMBER/GITHUB_TOKEN).")
        sys.exit(1)

    if not all([jira_url, jira_user, jira_token]):
        print("Error: JIRA credentials are not set. Make sure JIRA_URL, JIRA_USERNAME, and JIRA_API_TOKEN are configured.")
        sys.exit(1)

    print(f"Checking restrictions for repository: {repo}, target branch: {base_branch}")

    # Extract project name from repo (assuming format "owner/project-name")
    project_name = repo.split('/')[-1] if '/' in repo else repo

    # Create temporary directory and clone manifest repo
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        try:
            manifest_dir = clone_manifest_repo(temp_path)
        except Exception as e:
            print(f"Error: Failed to clone manifest repository: {e}")
            sys.exit(1)

        # Find restricted manifests that reference this project/branch
        restricted_manifests = get_restricted_manifests(project_name, base_branch, manifest_dir)

        if not restricted_manifests:
            print(f"✅ Branch '{base_branch}' for project '{project_name}' is not part of any restricted release manifest. Skipping extra checks.")
            sys.exit(0)

        print(f"Found {len(restricted_manifests)} restricted manifest(s) that reference this project/branch:")
        for manifest in restricted_manifests:
            print(f"  - {manifest['manifest_path']} (approval ticket: {manifest['approval_ticket']})")

        # Get JIRA keys from commit messages
        jira_keys = get_jira_keys_from_commits(repo, pr_number, gh_token)
        print(f"JIRA references found in commit messages: {', '.join(sorted(jira_keys)) if jira_keys else 'None'}")

        if not jira_keys:
            print("❌ No JIRA ticket reference found in any commit message. Please include a JIRA issue key in at least one commit message.")
            sys.exit(1)

        # Check approval for each restricted manifest
        all_approved = True
        for manifest in restricted_manifests:
            approval_ticket = manifest['approval_ticket']
            release_name = manifest['release_name']

            print(f"\nChecking approval for manifest {manifest['manifest_path']} (approval ticket: {approval_ticket})")

            # Get approved JIRA keys for this manifest
            approved_keys = get_approved_jira_keys(approval_ticket, jira_url, jira_user, jira_token)

            if not approved_keys:
                print(f"❌ Could not retrieve approved tickets for {approval_ticket}")
                all_approved = False
                continue

            # Check if all commit JIRA keys are approved
            not_approved = [key for key in jira_keys if key not in approved_keys]
            if not_approved:
                print(f"❌ The following JIRA ticket(s) are not approved for {release_name}: {', '.join(not_approved)}. "
                      f"Please link these issue(s) in the approval ticket {approval_ticket} before merging.")
                all_approved = False
            else:
                print(f"✅ All JIRA tickets are approved for {release_name}")

        if not all_approved:
            sys.exit(1)

        print("\n✅ All checks passed. All JIRA tickets referenced in commits are approved for all restricted manifests.")


if __name__ == "__main__":
    main()