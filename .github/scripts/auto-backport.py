#!/usr/bin/env python3

import argparse
import os
import re
import sys
import tempfile
import logging
from packaging import version

from github import Github, GithubException
from git import Repo, GitCommandError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
try:
    github_token = os.environ["GITHUB_TOKEN"]
except KeyError:
    print("Please set the 'GITHUB_TOKEN' environment variable")
    sys.exit(1)


def is_pull_request():
    return '--pull-request' in sys.argv[1:]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=str, required=True, help='Github repository name')
    parser.add_argument('--base-branch', type=str, default='refs/heads/next', help='Base branch')
    parser.add_argument('--commits', default=None, type=str, help='Range of promoted commits.')
    parser.add_argument('--pull-request', type=int, help='Pull request number to be backported')
    parser.add_argument('--head-commit', type=str, required=is_pull_request(), help='The HEAD of target branch after the pull request specified by --pull-request is merged')
    parser.add_argument('--label', type=str, required=is_pull_request(), help='Backport label name when --pull-request is defined')
    return parser.parse_args()


def create_pull_request(repo, new_branch_name, base_branch_name, pr, backport_pr_title, commits, is_draft=False):
    pr_body = f'{pr.body}\n\n'
    for commit in commits:
        pr_body += f'- (cherry picked from commit {commit})\n\n'
    pr_body += f'Parent PR: #{pr.number}'
    try:
        backport_pr = repo.create_pull(
            title=backport_pr_title,
            body=pr_body,
            head=f'scylladbbot:{new_branch_name}',
            base=base_branch_name,
            draft=is_draft
        )
        logging.info(f"Pull request created: {backport_pr.html_url}")
        backport_pr.add_to_assignees(pr.user)
        
        # Add labels to the backport PR
        labels_to_add = []
        
        # Check for priority labels (P0 or P1) in parent PR and add them to backport PR
        priority_labels = {"P0", "P1"}
        parent_pr_labels = [label.name for label in pr.labels]
        for label in priority_labels:
            if label in parent_pr_labels:
                labels_to_add.append(label)
                labels_to_add.append("force_on_cloud")
                logging.info(f"Adding {label} and force_on_cloud labels from parent PR to backport PR")
                break  # Only apply the highest priority label
        
        # Add conflicts label if PR is in draft mode
        if is_draft:
            labels_to_add.append("conflicts")
            pr_comment = f"@{pr.user.login} - This PR has conflicts, therefore it was moved to `draft` \n"
            pr_comment += "Please resolve them and mark this PR as ready for review"
            backport_pr.create_issue_comment(pr_comment)
            
        # Apply all labels at once if we have any
        if labels_to_add:
            backport_pr.add_to_labels(*labels_to_add)
            logging.info(f"Added labels to backport PR: {labels_to_add}")
            
        logging.info(f"Assigned PR to original author: {pr.user}")
        return backport_pr
    except GithubException as e:
        if 'A pull request already exists' in str(e):
            logging.warning(f'A pull request already exists for {pr.user}:{new_branch_name}')
        else:
            logging.error(f'Failed to create PR: {e}')


def get_pr_commits(repo, pr, stable_branch, start_commit=None):
    commits = []
    if pr.merged:
        merge_commit = repo.get_commit(pr.merge_commit_sha)
        if len(merge_commit.parents) > 1:  # Check if this merge commit includes multiple commits
            commits.append(pr.merge_commit_sha)
        else:
            if start_commit:
                promoted_commits = repo.compare(start_commit, stable_branch).commits
            else:
                promoted_commits = repo.get_commits(sha=stable_branch)
            for commit in pr.get_commits():
                for promoted_commit in promoted_commits:
                    commit_title = commit.commit.message.splitlines()[0]
                    # In Scylla-pkg and scylla-dtest, for example,
                    # we don't create a merge commit for a PR with multiple commits,
                    # according to the GitHub API, the last commit will be the merge commit,
                    # which is not what we need when backporting (we need all the commits).
                    # So here, we are validating the correct SHA for each commit so we can cherry-pick
                    if promoted_commit.commit.message.startswith(commit_title):
                        commits.append(promoted_commit.sha)

    elif pr.state == 'closed':
        events = pr.get_issue_events()
        for event in events:
            if event.event == 'closed':
                commits.append(event.commit_id)
    return commits


def backport(repo, pr, version, commits, backport_base_branch):
    new_branch_name = f'backport/{pr.number}/to-{version}'
    backport_pr_title = f'[Backport {version}] {pr.title}'
    repo_url = f'https://scylladbbot:{github_token}@github.com/{repo.full_name}.git'
    fork_repo = f'https://scylladbbot:{github_token}@github.com/scylladbbot/{repo.name}.git'
    with (tempfile.TemporaryDirectory() as local_repo_path):
        try:
            repo_local = Repo.clone_from(repo_url, local_repo_path, branch=backport_base_branch)
            repo_local.git.checkout(b=new_branch_name)
            is_draft = False
            for commit in commits:
                try:
                    repo_local.git.cherry_pick(commit, '-m1', '-x')
                except GitCommandError as e:
                    logging.warning(f'Cherry-pick conflict on commit {commit}: {e}')
                    is_draft = True
                    repo_local.git.add(A=True)
                    repo_local.git.cherry_pick('--continue')
            repo_local.git.push(fork_repo, new_branch_name, force=True)
            backport_pr = create_pull_request(repo, new_branch_name, backport_base_branch, pr, backport_pr_title, commits,
                                is_draft=is_draft)
            return backport_pr
        except GitCommandError as e:
            logging.warning(f"GitCommandError: {e}")
            return None


def sort_backport_labels(backport_labels):
    """Sort backport labels by version, latest first"""
    def extract_version(label):
        version_str = label.replace('backport/', '')
        return version.parse(version_str)
    
    return sorted(backport_labels, key=extract_version, reverse=True)


def update_backport_label(repo, parent_pr, backport_pr, label):
    """Update backport label to done on both parent and backport PRs"""
    new_label = label + '-done'
    for pr in [parent_pr, backport_pr]:
        try:
            pr.remove_from_labels(label)
            pr.add_to_labels(new_label)
            logging.info(f"Updated label from '{label}' to '{new_label}' on PR #{pr.number}")
        except GithubException as e:
            logging.error(f"Failed to update label on PR #{pr.number}: {e}")


def get_commits_from_newer_release(previous_backport_pr):
    """Get commits from the previous backport PR for subsequent backports"""
    commits = []
    for commit in previous_backport_pr.get_commits():
        commits.append(commit.sha)
    return commits


def create_pr_comment_and_remove_label(pr):
    comment_body = f':warning:  @{pr.user.login} PR body does not contain a valid reference to an issue '
    comment_body += ' based on [linking-a-pull-request-to-an-issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue#linking-a-pull-request-to-an-issue-using-a-keyword)'
    comment_body += ' and can not be backported\n\n'
    comment_body += 'The following labels were removed:\n'
    labels = pr.get_labels()
    pattern = re.compile(r"backport/\d+\.\d+$")
    for label in labels:
        if pattern.match(label.name):
            print(f"Removing label: {label.name}")
            comment_body += f'- {label.name}\n'
            pr.remove_from_labels(label)
    comment_body += f'\nPlease add the relevant backport labels after PR body is fixed'
    pr.create_issue_comment(comment_body)


def main():
    args = parse_args()
    base_branch = args.base_branch.split('/')[2]
    promoted_label = 'promoted-to-master'
    repo_name = args.repo

    backport_branch = 'next-'
    stable_branch = 'master' if base_branch == 'next' else base_branch.replace('next', 'branch')
    backport_label_pattern = re.compile(r'backport/\d+\.\d+$')

    g = Github(github_token)
    repo = g.get_repo(repo_name)
    closed_prs = []
    start_commit = None

    if args.commits:
        start_commit, end_commit = args.commits.split('..')
        commits = repo.compare(start_commit, end_commit).commits
        for commit in commits:
            for pr in commit.get_pulls():
                closed_prs.append(pr)
    if args.pull_request:
        start_commit = args.head_commit
        pr = repo.get_pull(args.pull_request)
        closed_prs = [pr]

    for pr in closed_prs:
        labels = [label.name for label in pr.labels]
        if args.pull_request:
            if args.label:
                backport_labels = [args.label]
            else:
                backport_labels = [label for label in labels if backport_label_pattern.match(label)]
        else:
            backport_labels = [label for label in labels if backport_label_pattern.match(label)]
        
        if promoted_label not in labels and not args.pull_request:
            print(f'no {promoted_label} label: {pr.number}')
            continue
        if not backport_labels:
            print(f'no backport label: {pr.number}')
            continue
        
        # Sort backport labels by version (latest first)
        sorted_backport_labels = sort_backport_labels(backport_labels)
        logging.info(f"Sorted backport labels for PR #{pr.number}: {sorted_backport_labels}")
        
        commits = get_pr_commits(repo, pr, stable_branch, start_commit)
        logging.info(f"Found PR #{pr.number} with commits {commits}")
        
        # Process backports sequentially (latest version first)
        previous_backport_pr = None
        for i, backport_label in enumerate(sorted_backport_labels):
            version = backport_label.replace('backport/', '')
            backport_base_branch = backport_label.replace('backport/', backport_branch)
            
            # For subsequent backports (not the first one), get commits from the previous backport PR
            if i > 0 and previous_backport_pr:
                backport_commits = get_commits_from_newer_release(previous_backport_pr)
                logging.info(f"Using commits from previous backport PR for backport to {version}: {backport_commits}")
            else:
                backport_commits = commits
            
            logging.info(f"Starting backport to {version} for PR #{pr.number}")
            backport_pr = backport(repo, pr, version, backport_commits, backport_base_branch)
            
            if backport_pr:
                # Update labels to mark this backport as done
                done_label = f"{backport_label}-done"
                update_backport_label(repo, pr, backport_pr, backport_label)
                
                previous_backport_pr = backport_pr
                logging.info(f"Completed backport to {version} for PR #{pr.number}")
            else:
                logging.error(f"Failed to create backport PR for {version}")


if __name__ == "__main__":
    main()
