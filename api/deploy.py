"""
Holds routes for deployment based off of Github events
"""
import hmac
import logging
import re
import shutil
from pathlib import Path

from faf.tools.fa.build_mod import build_mod
from faf.tools.fa.mods import parse_mod_info

from api.oauth_handlers import *
from .git import checkout_repo

logger = logging.getLogger(__name__)

github_session = None


def validate_github_request(data, signature):
    digest = hmac.new(app.config['GITHUB_SECRET'],
                      data, 'sha1').hexdigest()
    return hmac.compare_digest(digest, signature)


@app.route('/deployment/<repo>/<int:deployment_id>', methods=['GET'])
def deployment(repo, deployment_id):
    return app.github.deployment(owner='FAForever', repo=repo, id=deployment_id).json()


@app.route('/status/<repo>', methods=['GET'])
def deployments(repo):
    return {
        'status': 'OK',
        'deployments': app.github.deployments(owner='FAForever', repo=repo).json()
    }


@app.route('/github', methods=['POST'])
def github_hook():
    """
    Generic github hook suitable for receiving github status events.
    :return:
    """

    #  Validate that we have a a legitimate github request
    # if not validate_github_request(request.data, request.headers['X-Hub-Signature'].split("sha1=")[1]):
    #   return dict(status="Invalid request"), 400

    event = request.headers['X-Github-Event']

    if event == 'push':
        """
        body_deployment['id'] is a numeric code identifying the deployment
        body_deployment['environment'] is the environment to deploy to. Defaults to production
        """

        body = request.get_json()
        branch = body['ref']
        game_mode = app.config['DEPLOY_BRANCHES'][branch]

        if game_mode:
            repo = body['repository']
            commit = body['after']

            # Build mod on database from git and write to download system
            status, description = deploy_route(repo['name'],
                                               branch,
                                               game_mode,
                                               commit)
            # Create status update on github
            status_response = app.github.create_deployment_status(owner='FAForever',
                                                                  repo=repo['name'],
                                                                  id=repo['id'],
                                                                  state=status,
                                                                  description=description)
            # Create status update on Slack
            app.slack.send_message(username='deploybot',
                                   text="Deployed {}:{} to {}".format(
                                       repo['name'],
                                       "{}@{}".format(branch, commit),
                                       game_mode))
            # Create status responses
            if status_response.status_code == 201:
                return (dict(status=status,
                             description=description),
                        201)
            else:
                return ((dict(status='error',
                              description="Failure creating github deployment status: {}"
                              .format(status_response.content))),
                        status_response.status_code)
    return dict(status="OK"), 200


def deploy_web(repo_path: Path, remote_url: Path, ref: str, sha: str):
    checkout_repo(repo_path, remote_url, ref, sha)
    restart_file = Path(repo_path, 'tmp/restart.txt')
    restart_file.touch()
    return 'success', 'Deployed'


def deploy_game(repo_path: Path, remote_url: Path, branch: str, game_mode: str, sha: str):
    checkout_repo(repo_path, remote_url, branch, sha)  # Checkout the intended state on the server repo

    mod_info = parse_mod_info(Path(repo_path, 'mod_info.lua'))  # Harvest data from mod_info.lua
    version = str(mod_info['version'])

    files = build_mod(repo_path)  # Build the mod from the fileset we just checked out
    logger.info('Build result: {}'.format(files))

    deploy_path = Path(app.config['GAME_DEPLOY_PATH'], 'updates_{}_files'.format(game_mode))
    logger.info('Deploying {} to {}'.format(game_mode, deploy_path))

    for file in files:
        # Organise the files needed into their final setup and pack as .zip
        destination = deploy_path / (file['filename'] + '.' + game_mode + '.' + version + file['sha1'][:6] + '.zip')
        logger.info('Deploying {} to {}'.format(file, destination))
        shutil.copy2(str(file['path']), str(destination))

        # Update the database with the new mod
        db.execute_sql('delete from updates_{}_files where fileId = %s and version = %s;'.format(game_mode),
                       (file['id'], version))  # Out with the old
        db.execute_sql('insert into updates_{}_files '
                       '(fileId, version, md5, name) '
                       'values (%s,%s,%s,%s)'.format(game_mode),
                       (file['id'], version, file['md5'], destination.name))  # In with the new

    return 'Success', 'Deployed ' + repository + ' branch ' + branch + ' to ' + game_mode


def deploy_route(repository, branch, game_mode, commit):
    """
    Perform deployment on this machine
    :param repository: the source repository
    :param branch: the source branch
    :param game_mode: the game mode we are deploying to
    :param commit: commit hash to verify deployment
    :return: (status: str, description: str)
    """

    github_url = app.config['GIT_URL']
    remote_url = github_url + repository + '.git'

    try:
        return {
            'api': deploy_web,
            'patchnotes': deploy_web,
            'fa': deploy_game
        }[repository](Path(app.config['REPO_PATHS'][repository]), remote_url, branch, game_mode, commit)
    except Exception as e:
        logger.exception(e)
        return 'error', "{}: {}".format(type(e), e)
