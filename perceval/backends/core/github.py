# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2019 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, 51 Franklin Street, Fifth Floor, Boston, MA 02110-1335, USA.
#
# Authors:
#     Alvaro del Castillo San Felix <acs@bitergia.com>
#     Santiago Dueñas <sduenas@bitergia.com>
#     Alberto Martín <alberto.martin@bitergia.com>
#

import json
import logging

import requests
from grimoirelab_toolkit.datetime import (datetime_to_utc,
                                          datetime_utcnow,
                                          str_to_datetime)
from grimoirelab_toolkit.uris import urijoin

from ...backend import (Backend,
                        BackendCommand,
                        BackendCommandArgumentParser)
from ...client import HttpClient, RateLimitHandler
from ...utils import DEFAULT_DATETIME, DEFAULT_LAST_DATETIME

CATEGORY_ISSUE = "issue"
CATEGORY_PULL_REQUEST = "pull_request"
CATEGORY_REPO = 'repository'

GITHUB_URL = "https://github.com/"
GITHUB_API_URL = "https://api.github.com"

# Range before sleeping until rate limit reset
MIN_RATE_LIMIT = 10
MAX_RATE_LIMIT = 500
# Use this factor of the current token's remaining API points before switching to the next token
TOKEN_USAGE_BEFORE_SWITCH = 0.1

MAX_CATEGORY_ITEMS_PER_PAGE = 100
PER_PAGE = 100

# Default sleep time and retries to deal with connection/server problems
DEFAULT_SLEEP_TIME = 1
MAX_RETRIES = 5

TARGET_ISSUE_FIELDS = ['user', 'assignee', 'assignees', 'comments', 'reactions']
TARGET_PULL_FIELDS = ['user', 'review_comments', 'requested_reviewers', "merged_by", "commits"]

logger = logging.getLogger(__name__)


class GitHub(Backend):
    """GitHub backend for Perceval.

    This class allows the fetch the issues stored in GitHub
    repository.

    :param owner: GitHub owner
    :param repository: GitHub repository from the owner
    :param api_token: list of GitHub auth tokens to access the API
    :param base_url: GitHub URL in enterprise edition case;
        when no value is set the backend will be fetch the data
        from the GitHub public site.
    :param tag: label used to mark the data
    :param archive: archive to store/retrieve items
    :param sleep_for_rate: sleep until rate limit is reset
    :param min_rate_to_sleep: minimun rate needed to sleep until
         it will be reset
    :param max_retries: number of max retries to a data source
        before raising a RetryError exception
    :param max_items: max number of category items (e.g., issues,
        pull requests) per query
    :param sleep_time: time to sleep in case
        of connection problems
    """
    version = '0.22.1'

    CATEGORIES = [CATEGORY_ISSUE, CATEGORY_PULL_REQUEST, CATEGORY_REPO]

    def __init__(self, owner=None, repository=None,
                 api_token=None, base_url=None,
                 tag=None, archive=None,
                 sleep_for_rate=False, min_rate_to_sleep=MIN_RATE_LIMIT,
                 max_retries=MAX_RETRIES, sleep_time=DEFAULT_SLEEP_TIME,
                 max_items=MAX_CATEGORY_ITEMS_PER_PAGE):
        if api_token is None:
            api_token = []
        origin = base_url if base_url else GITHUB_URL
        origin = urijoin(origin, owner, repository)

        super().__init__(origin, tag=tag, archive=archive)

        self.owner = owner
        self.repository = repository
        self.api_token = api_token
        self.base_url = base_url

        self.sleep_for_rate = sleep_for_rate
        self.min_rate_to_sleep = min_rate_to_sleep
        self.max_retries = max_retries
        self.sleep_time = sleep_time
        self.max_items = max_items

        self.client = None
        self._users = {}  # internal users cache

    def fetch(self, category=CATEGORY_ISSUE, from_date=DEFAULT_DATETIME, to_date=DEFAULT_LAST_DATETIME):
        """Fetch the issues/pull requests from the repository.

        The method retrieves, from a GitHub repository, the issues/pull requests
        updated since the given date.

        :param category: the category of items to fetch
        :param from_date: obtain issues/pull requests updated since this date
        :param to_date: obtain issues/pull requests until a specific date (included)

        :returns: a generator of issues
        """
        if not from_date:
            from_date = DEFAULT_DATETIME
        if not to_date:
            to_date = DEFAULT_LAST_DATETIME

        from_date = datetime_to_utc(from_date)
        to_date = datetime_to_utc(to_date)

        kwargs = {
            'from_date': from_date,
            'to_date': to_date
        }
        items = super().fetch(category, **kwargs)

        return items

    def fetch_items(self, category, **kwargs):
        """Fetch the items (issues or pull_requests)

        :param category: the category of items to fetch
        :param kwargs: backend arguments

        :returns: a generator of items
        """
        from_date = kwargs['from_date']
        to_date = kwargs['to_date']

        if category == CATEGORY_ISSUE:
            items = self.__fetch_issues(from_date, to_date)
        elif category == CATEGORY_PULL_REQUEST:
            items = self.__fetch_pull_requests(from_date, to_date)
        else:
            items = self.__fetch_repo_info()

        return items

    @classmethod
    def has_archiving(cls):
        """Returns whether it supports archiving items on the fetch process.

        :returns: this backend supports items archive
        """
        return True

    @classmethod
    def has_resuming(cls):
        """Returns whether it supports to resume the fetch process.

        :returns: this backend supports items resuming
        """
        return True

    @staticmethod
    def metadata_id(item):
        """Extracts the identifier from a GitHub item."""

        if "forks_count" in item:
            return str(item['fetched_on'])
        else:
            return str(item['id'])

    @staticmethod
    def metadata_updated_on(item):
        """Extracts the update time from a GitHub item.

        The timestamp used is extracted from 'updated_at' field.
        This date is converted to UNIX timestamp format. As GitHub
        dates are in UTC the conversion is straightforward.

        :param item: item generated by the backend

        :returns: a UNIX timestamp
        """
        if "forks_count" in item:
            return item['fetched_on']
        else:
            ts = item['updated_at']
            ts = str_to_datetime(ts)

            return ts.timestamp()

    @staticmethod
    def metadata_category(item):
        """Extracts the category from a GitHub item.

        This backend generates two types of item which are
        'issue' and 'pull_request'.
        """

        if "base" in item:
            category = CATEGORY_PULL_REQUEST
        elif "forks_count" in item:
            category = CATEGORY_REPO
        else:
            category = CATEGORY_ISSUE

        return category

    def _init_client(self, from_archive=False):
        """Init client"""

        return GitHubClient(self.owner, self.repository, self.api_token, self.base_url,
                            self.sleep_for_rate, self.min_rate_to_sleep,
                            self.sleep_time, self.max_retries, self.max_items,
                            self.archive, from_archive)

    def __fetch_issues(self, from_date, to_date):
        """Fetch the issues"""

        issues_groups = self.client.issues(from_date=from_date)

        for raw_issues in issues_groups:
            issues = json.loads(raw_issues)
            for issue in issues:

                if str_to_datetime(issue['updated_at']) > to_date:
                    return

                self.__init_extra_issue_fields(issue)
                for field in TARGET_ISSUE_FIELDS:

                    if not issue[field]:
                        continue

                    if field == 'user':
                        issue[field + '_data'] = self.__get_user(issue[field]['login'])
                    elif field == 'assignee':
                        issue[field + '_data'] = self.__get_issue_assignee(issue[field])
                    elif field == 'assignees':
                        issue[field + '_data'] = self.__get_issue_assignees(issue[field])
                    elif field == 'comments':
                        issue[field + '_data'] = self.__get_issue_comments(issue['number'])
                    elif field == 'reactions':
                        issue[field + '_data'] = \
                            self.__get_issue_reactions(issue['number'], issue['reactions']['total_count'])

                yield issue

    def __fetch_pull_requests(self, from_date, to_date):
        """Fetch the pull requests"""

        raw_pulls = self.client.pulls(from_date=from_date)
        for raw_pull in raw_pulls:
            pull = json.loads(raw_pull)

            if str_to_datetime(pull['updated_at']) > to_date:
                return

            self.__init_extra_pull_fields(pull)

            pull['reviews_data'] = self.__get_pull_reviews(pull['number'])

            for field in TARGET_PULL_FIELDS:
                if not pull[field]:
                    continue

                if field == 'user':
                    pull[field + '_data'] = self.__get_user(pull[field]['login'])
                elif field == 'merged_by':
                    pull[field + '_data'] = self.__get_user(pull[field]['login'])
                elif field == 'review_comments':
                    pull[field + '_data'] = self.__get_pull_review_comments(pull['number'])
                elif field == 'requested_reviewers':
                    pull[field + '_data'] = self.__get_pull_requested_reviewers(pull['number'])
                elif field == 'commits':
                    pull[field + '_data'] = self.__get_pull_commits(pull['number'])

            yield pull

    def __fetch_repo_info(self):
        """Get repo info about stars, watchers and forks"""

        raw_repo = self.client.repo()
        repo = json.loads(raw_repo)

        fetched_on = datetime_utcnow()
        repo['fetched_on'] = fetched_on.timestamp()

        yield repo

    def __get_issue_reactions(self, issue_number, total_count):
        """Get issue reactions"""

        reactions = []

        if total_count == 0:
            return reactions

        group_reactions = self.client.issue_reactions(issue_number)

        for raw_reactions in group_reactions:

            for reaction in json.loads(raw_reactions):
                reaction['user_data'] = self.__get_user(reaction['user']['login'])
                reactions.append(reaction)

        return reactions

    def __get_issue_comments(self, issue_number):
        """Get issue comments"""

        comments = []
        group_comments = self.client.issue_comments(issue_number)

        for raw_comments in group_comments:

            for comment in json.loads(raw_comments):
                comment_id = comment.get('id')
                comment['user_data'] = self.__get_user(comment['user']['login'])
                comment['reactions_data'] = \
                    self.__get_issue_comment_reactions(comment_id, comment['reactions']['total_count'])
                comments.append(comment)

        return comments

    def __get_issue_comment_reactions(self, comment_id, total_count):
        """Get reactions on issue comments"""

        reactions = []

        if total_count == 0:
            return reactions

        group_reactions = self.client.issue_comment_reactions(comment_id)

        for raw_reactions in group_reactions:

            for reaction in json.loads(raw_reactions):
                reaction['user_data'] = self.__get_user(reaction['user']['login'])
                reactions.append(reaction)

        return reactions

    def __get_issue_assignee(self, raw_assignee):
        """Get issue assignee"""

        assignee = self.__get_user(raw_assignee['login'])

        return assignee

    def __get_issue_assignees(self, raw_assignees):
        """Get issue assignees"""

        assignees = []
        for ra in raw_assignees:
            assignees.append(self.__get_user(ra['login']))

        return assignees

    def __get_pull_requested_reviewers(self, pr_number):
        """Get pull request requested reviewers"""

        requested_reviewers = []
        group_requested_reviewers = self.client.pull_requested_reviewers(pr_number)

        for raw_requested_reviewers in group_requested_reviewers:
            group_requested_reviewers = json.loads(raw_requested_reviewers)

            # GH Enterprise returns list of users instead of dict (issue #523)
            if isinstance(group_requested_reviewers, list):
                group_requested_reviewers = {'users': group_requested_reviewers}

            for requested_reviewer in group_requested_reviewers['users']:
                user_data = self.__get_user(requested_reviewer['login'])
                requested_reviewers.append(user_data)

        return requested_reviewers

    def __get_pull_commits(self, pr_number):
        """Get pull request commit hashes"""

        hashes = []
        group_pull_commits = self.client.pull_commits(pr_number)

        for raw_pull_commits in group_pull_commits:

            for commit in json.loads(raw_pull_commits):
                commit_hash = commit['sha']
                hashes.append(commit_hash)

        return hashes

    def __get_pull_review_comments(self, pr_number):
        """Get pull request review comments"""

        comments = []
        group_comments = self.client.pull_review_comments(pr_number)

        for raw_comments in group_comments:

            for comment in json.loads(raw_comments):
                comment_id = comment.get('id')

                user = comment.get('user', None)
                if not user:
                    logger.warning("Missing user info for %s", comment['url'])
                    comment['user_data'] = None
                else:
                    comment['user_data'] = self.__get_user(user['login'])

                comment['reactions_data'] = \
                    self.__get_pull_review_comment_reactions(comment_id, comment['reactions']['total_count'])
                comments.append(comment)

        return comments

    def __get_pull_reviews(self, pr_number):
        """Get pull request reviews"""

        reviews = []
        group_reviews = self.client.pull_reviews(pr_number)

        for raw_reviews in group_reviews:

            for review in json.loads(raw_reviews):
                user = review.get('user', None)
                if not user:
                    logger.warning("Missing user info for %s", review['html_url'])
                    review['user_data'] = None
                else:
                    review['user_data'] = self.__get_user(user['login'])

                reviews.append(review)
        return reviews

    def __get_pull_review_comment_reactions(self, comment_id, total_count):
        """Get pull review comment reactions"""

        reactions = []

        if total_count == 0:
            return reactions

        group_reactions = self.client.pull_review_comment_reactions(comment_id)

        for raw_reactions in group_reactions:

            for reaction in json.loads(raw_reactions):
                reaction['user_data'] = self.__get_user(reaction['user']['login'])
                reactions.append(reaction)

        return reactions

    def __get_user(self, login):
        """Get user and org data for the login"""

        user = {}

        if not login:
            return user

        user_raw = self.client.user(login)
        user = json.loads(user_raw)
        user_orgs_raw = \
            self.client.user_orgs(login)
        user['organizations'] = json.loads(user_orgs_raw)

        return user

    def __init_extra_issue_fields(self, issue):
        """Add fields to an issue"""

        issue['user_data'] = {}
        issue['assignee_data'] = {}
        issue['assignees_data'] = []
        issue['comments_data'] = []
        issue['reactions_data'] = []

    def __init_extra_pull_fields(self, pull):
        """Add fields to a pull request"""

        pull['user_data'] = {}
        pull['review_comments_data'] = {}
        pull['reviews_data'] = []
        pull['requested_reviewers_data'] = []
        pull['merged_by_data'] = []
        pull['commits_data'] = []


class GitHubClient(HttpClient, RateLimitHandler):
    """Client for retieving information from GitHub API

    :param owner: GitHub owner
    :param repository: GitHub repository from the owner
    :param tokens: list of GitHub auth tokens to access the API
    :param base_url: GitHub URL in enterprise edition case;
        when no value is set the backend will be fetch the data
        from the GitHub public site.
    :param sleep_for_rate: sleep until rate limit is reset
    :param min_rate_to_sleep: minimun rate needed to sleep until
         it will be reset
    :param sleep_time: time to sleep in case
        of connection problems
    :param max_retries: number of max retries to a data source
        before raising a RetryError exception
    :param max_items: max number of category items (e.g., issues,
        pull requests) per query
    :param archive: collect issues already retrieved from an archive
    :param from_archive: it tells whether to write/read the archive
    """
    EXTRA_STATUS_FORCELIST = [403, 500, 502, 503]

    _users = {}       # users cache
    _users_orgs = {}  # users orgs cache

    def __init__(self, owner, repository, tokens,
                 base_url=None, sleep_for_rate=False, min_rate_to_sleep=MIN_RATE_LIMIT,
                 sleep_time=DEFAULT_SLEEP_TIME, max_retries=MAX_RETRIES,
                 max_items=MAX_CATEGORY_ITEMS_PER_PAGE, archive=None, from_archive=False):
        self.owner = owner
        self.repository = repository
        self.tokens = tokens
        self.n_tokens = len(self.tokens)
        self.current_token = None
        self.last_rate_limit_checked = None
        self.max_items = max_items

        if base_url:
            base_url = urijoin(base_url, 'api', 'v3')
        else:
            base_url = GITHUB_API_URL

        super().__init__(base_url, sleep_time=sleep_time, max_retries=max_retries,
                         extra_headers=self._set_extra_headers(),
                         extra_status_forcelist=self.EXTRA_STATUS_FORCELIST,
                         archive=archive, from_archive=from_archive)
        super().setup_rate_limit_handler(sleep_for_rate=sleep_for_rate, min_rate_to_sleep=min_rate_to_sleep)

        # Choose best API token (with maximum API points remaining)
        if not self.from_archive:
            self._choose_best_api_token()

    def calculate_time_to_reset(self):
        """Calculate the seconds to reset the token requests, by obtaining the different
        between the current date and the next date when the token is fully regenerated.
        """

        time_to_reset = self.rate_limit_reset_ts - (datetime_utcnow().replace(microsecond=0).timestamp() + 1)
        time_to_reset = 0 if time_to_reset < 0 else time_to_reset

        return time_to_reset

    def issue_reactions(self, issue_number):
        """Get reactions of an issue"""

        payload = {
            'per_page': PER_PAGE,
            'direction': 'asc',
            'sort': 'updated'
        }

        path = urijoin("issues", str(issue_number), "reactions")
        return self.fetch_items(path, payload)

    def issue_comment_reactions(self, comment_id):
        """Get reactions of an issue comment"""

        payload = {
            'per_page': PER_PAGE,
            'direction': 'asc',
            'sort': 'updated'
        }

        path = urijoin("issues", "comments", str(comment_id), "reactions")
        return self.fetch_items(path, payload)

    def issue_comments(self, issue_number):
        """Get the issue comments from pagination"""

        payload = {
            'per_page': PER_PAGE,
            'direction': 'asc',
            'sort': 'updated'
        }

        path = urijoin("issues", str(issue_number), "comments")
        return self.fetch_items(path, payload)

    def issues(self, from_date=None):
        """Fetch the issues from the repository.

        The method retrieves, from a GitHub repository, the issues
        updated since the given date.

        :param from_date: obtain issues updated since this date

        :returns: a generator of issues
        """
        payload = {
            'state': 'all',
            'per_page': self.max_items,
            'direction': 'asc',
            'sort': 'updated'}

        if from_date:
            payload['since'] = from_date.isoformat()

        path = urijoin("issues")
        return self.fetch_items(path, payload)

    def pulls(self, from_date=None):
        """Fetch the pull requests from the repository.

        The method retrieves, from a GitHub repository, the pull requests
        updated since the given date.

        :param from_date: obtain pull requests updated since this date

        :returns: a generator of pull requests
        """
        issues_groups = self.issues(from_date=from_date)

        for raw_issues in issues_groups:
            issues = json.loads(raw_issues)
            for issue in issues:

                if "pull_request" not in issue:
                    continue

                pull_number = issue["number"]
                path = urijoin(self.base_url, 'repos', self.owner, self.repository, "pulls", pull_number)
                r = self.fetch(path)
                pull = r.text
                yield pull

    def repo(self):
        """Get repository data"""

        path = urijoin(self.base_url, 'repos', self.owner, self.repository)

        r = self.fetch(path)
        repo = r.text

        return repo

    def pull_requested_reviewers(self, pr_number):
        """Get pull requested reviewers"""

        requested_reviewers_url = urijoin("pulls", str(pr_number), "requested_reviewers")
        return self.fetch_items(requested_reviewers_url, {})

    def pull_commits(self, pr_number):
        """Get pull request commits"""

        payload = {
            'per_page': PER_PAGE,
        }

        commit_url = urijoin("pulls", str(pr_number), "commits")
        return self.fetch_items(commit_url, payload)

    def pull_review_comments(self, pr_number):
        """Get pull request review comments"""

        payload = {
            'per_page': PER_PAGE,
            'direction': 'asc',
            'sort': 'updated'
        }

        comments_url = urijoin("pulls", str(pr_number), "comments")
        return self.fetch_items(comments_url, payload)

    def pull_reviews(self, pr_number):
        """Get pull request reviews"""

        payload = {
            'per_page': PER_PAGE,
            'direction': 'asc',
            'sort': 'updated'
        }

        reviews_url = urijoin("pulls", str(pr_number), "reviews")
        return self.fetch_items(reviews_url, payload)

    def pull_review_comment_reactions(self, comment_id):
        """Get reactions of a review comment"""

        payload = {
            'per_page': PER_PAGE,
            'direction': 'asc',
            'sort': 'updated'
        }

        path = urijoin("pulls", "comments", str(comment_id), "reactions")
        return self.fetch_items(path, payload)

    def user(self, login):
        """Get the user information and update the user cache"""
        user = None

        if login in self._users:
            return self._users[login]

        url_user = urijoin(self.base_url, 'users', login)

        logging.info("Getting info for %s" % (url_user))

        r = self.fetch(url_user)
        user = r.text
        self._users[login] = user

        return user

    def user_orgs(self, login):
        """Get the user public organizations"""
        if login in self._users_orgs:
            return self._users_orgs[login]

        url = urijoin(self.base_url, 'users', login, 'orgs')
        try:
            r = self.fetch(url)
            orgs = r.text
        except requests.exceptions.HTTPError as error:
            # 404 not found is wrongly received sometimes
            if error.response.status_code == 404:
                logger.error("Can't get github login orgs: %s", error)
                orgs = '[]'
            else:
                raise error

        self._users_orgs[login] = orgs

        return orgs

    def fetch(self, url, payload=None, headers=None, method=HttpClient.GET, stream=False, verify=True):
        """Fetch the data from a given URL.

        :param url: link to the resource
        :param payload: payload of the request
        :param headers: headers of the request
        :param method: type of request call (GET or POST)
        :param stream: defer downloading the response body until the response content is available

        :returns a response object
        """
        if not self.from_archive:
            self.sleep_for_rate_limit()

        response = super().fetch(url, payload, headers, method, stream, verify)

        if not self.from_archive:
            if self._need_check_tokens():
                self._choose_best_api_token()
            else:
                self.update_rate_limit(response)

        return response

    def fetch_items(self, path, payload):
        """Return the items from github API using links pagination"""

        page = 0  # current page
        last_page = None  # last page
        url_next = urijoin(self.base_url, 'repos', self.owner, self.repository, path)
        logger.debug("Get GitHub paginated items from " + url_next)

        response = self.fetch(url_next, payload=payload)

        items = response.text
        page += 1

        if 'last' in response.links:
            last_url = response.links['last']['url']
            last_page = last_url.split('&page=')[1].split('&')[0]
            last_page = int(last_page)
            logger.debug("Page: %i/%i" % (page, last_page))

        while items:
            yield items

            items = None

            if 'next' in response.links:
                url_next = response.links['next']['url']
                response = self.fetch(url_next, payload=payload)
                page += 1

                items = response.text
                logger.debug("Page: %i/%i" % (page, last_page))

    def _get_token_rate_limit(self, token):
        """Return token's remaining API points"""

        rate_url = urijoin(self.base_url, "rate_limit")
        self.session.headers.update({'Authorization': 'token ' + token})
        remaining = 0
        try:
            headers = super().fetch(rate_url).headers
            if self.rate_limit_header in headers:
                remaining = int(headers[self.rate_limit_header])
        except requests.exceptions.HTTPError as error:
            logger.warning("Rate limit not initialized: %s", error)
        return remaining

    def _get_tokens_rate_limits(self):
        """Return array of all tokens remaining API points"""

        remainings = [0] * self.n_tokens
        # Turn off archiving when checking rates, because that would cause
        # archive key conflict (the same URLs giving different responses)
        arch = self.archive
        self.archive = None
        for idx, token in enumerate(self.tokens):
            # Pass flag to skip disabling archiving because this function doies it
            remainings[idx] = self._get_token_rate_limit(token)
        # Restore archiving to whatever state it was
        self.archive = arch
        logger.debug("Remaining API points: {}".format(remainings))
        return remainings

    def _choose_best_api_token(self):
        """Check all API tokens defined and choose one with most remaining API points"""

        # Return if no tokens given
        if self.n_tokens == 0:
            return

        # If multiple tokens given, choose best
        token_idx = 0
        if self.n_tokens > 1:
            remainings = self._get_tokens_rate_limits()
            token_idx = remainings.index(max(remainings))
            logger.debug("Remaining API points: {}, choosen index: {}".format(remainings, token_idx))

        # If we have any tokens - use best of them
        self.current_token = self.tokens[token_idx]
        self.session.headers.update({'Authorization': 'token ' + self.current_token})
        # Update rate limit data for the current token
        self._update_current_rate_limit()

    def _need_check_tokens(self):
        """Check if we need to switch GitHub API tokens"""

        if self.n_tokens <= 1 or self.rate_limit is None:
            return False
        elif self.last_rate_limit_checked is None:
            self.last_rate_limit_checked = self.rate_limit
            return True

        # If approaching minimum rate limit for sleep
        approaching_limit = float(self.min_rate_to_sleep) * (1.0 + TOKEN_USAGE_BEFORE_SWITCH) + 1
        if self.rate_limit <= approaching_limit:
            self.last_rate_limit_checked = self.rate_limit
            return True

        # Only switch token when used predefined factor of the current token's remaining API points
        ratio = float(self.rate_limit) / float(self.last_rate_limit_checked)
        if ratio < 1.0 - TOKEN_USAGE_BEFORE_SWITCH:
            self.last_rate_limit_checked = self.rate_limit
            return True
        elif ratio > 1.0:
            self.last_rate_limit_checked = self.rate_limit
            return False
        else:
            return False

    def _update_current_rate_limit(self):
        """Update rate limits data for the current token"""

        url = urijoin(self.base_url, "rate_limit")
        try:
            # Turn off archiving when checking rates, because that would cause
            # archive key conflict (the same URLs giving different responses)
            arch = self.archive
            self.archive = None
            response = super().fetch(url)
            self.archive = arch
            self.update_rate_limit(response)
            self.last_rate_limit_checked = self.rate_limit
        except requests.exceptions.HTTPError as error:
            if error.response.status_code == 404:
                logger.warning("Rate limit not initialized: %s", error)
            else:
                raise error

    def _set_extra_headers(self):
        """Set extra headers for session"""

        headers = {}
        headers.update({'Accept': 'application/vnd.github.squirrel-girl-preview'})
        return headers


class GitHubCommand(BackendCommand):
    """Class to run GitHub backend from the command line."""

    BACKEND = GitHub

    @classmethod
    def setup_cmd_parser(cls):
        """Returns the GitHub argument parser."""

        parser = BackendCommandArgumentParser(cls.BACKEND.CATEGORIES,
                                              from_date=True,
                                              to_date=True,
                                              token_auth=False,
                                              archive=True)
        # GitHub options
        group = parser.parser.add_argument_group('GitHub arguments')
        group.add_argument('--enterprise-url', dest='base_url',
                           help="Base URL for GitHub Enterprise instance")
        group.add_argument('--sleep-for-rate', dest='sleep_for_rate',
                           action='store_true',
                           help="sleep for getting more rate")
        group.add_argument('--min-rate-to-sleep', dest='min_rate_to_sleep',
                           default=MIN_RATE_LIMIT, type=int,
                           help="sleep until reset when the rate limit reaches this value")
        # GitHub token(s)
        group.add_argument('-t', '--api-token', dest='api_token',
                           nargs='+',
                           default=[],
                           help="list of GitHub API tokens")

        # Generic client options
        group.add_argument('--max-items', dest='max_items',
                           default=MAX_CATEGORY_ITEMS_PER_PAGE, type=int,
                           help="Max number of category items per query.")
        group.add_argument('--max-retries', dest='max_retries',
                           default=MAX_RETRIES, type=int,
                           help="number of API call retries")
        group.add_argument('--sleep-time', dest='sleep_time',
                           default=DEFAULT_SLEEP_TIME, type=int,
                           help="sleeping time between API call retries")

        # Positional arguments
        parser.parser.add_argument('owner',
                                   help="GitHub owner")
        parser.parser.add_argument('repository',
                                   help="GitHub repository")

        return parser
