#!/usr/bin/python
# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Manage user accounts on a Google Compute Engine instances."""

import datetime
import json
import logging.handlers
import optparse

from google_compute_engine import config_manager
from google_compute_engine import file_utils
from google_compute_engine import logger
from google_compute_engine import metadata_watcher
from google_compute_engine.accounts import accounts_utils

LOCKFILE = '/var/lock/google_accounts.lock'


class AccountsDaemon(object):
  """Manage user accounts based on changes to metadata."""

  invalid_users = set()

  def __init__(self, groups=None, remove=False, debug=False):
    """Constructor.

    Args:
      groups: string, a comma separated list of groups.
      remove: bool, True if deprovisioning a user should be destructive.
      debug: bool, True if debug output should write to the console.
    """
    facility = logging.handlers.SysLogHandler.LOG_DAEMON
    self.logger = logger.Logger(
        name='google-accounts', debug=debug, facility=facility)
    self.watcher = metadata_watcher.MetadataWatcher(logger=self.logger)
    self.utils = accounts_utils.AccountsUtils(
        logger=self.logger, groups=groups, remove=remove)
    try:
      with file_utils.LockFile(LOCKFILE):
        self.logger.info('Starting Google Accounts daemon.')
        self.watcher.WatchMetadata(self.HandleAccounts, recursive=True)
    except (IOError, OSError) as e:
      self.logger.warning(str(e))

  def _HasExpired(self, key):
    """Check whether an SSH key has expired.

    Uses Google-specific semantics of the OpenSSH public key format's comment
    field to determine if an SSH key is past its expiration timestamp, and
    therefore no longer to be trusted. This format is still subject to change.
    Reliance on it in any way is at your own risk.

    Args:
      key: string, a single public key entry in OpenSSH public key file format.
          This will be checked for Google-specific comment semantics, and if
          present, those will be analysed.

    Returns:
      bool, True if the key has Google-specific comment semantics and has an
          expiration timestamp in the past, or False otherwise.
    """
    self.logger.debug('Processing key: %s.', key)

    try:
      schema, json_str = key.split(None, 3)[2:]
    except (ValueError, AttributeError):
      self.logger.debug('No schema identifier. Not expiring key.')
      return False

    if schema != 'google-ssh':
      self.logger.debug('Invalid schema %s. Not expiring key.', schema)
      return False

    try:
      json_obj = json.loads(json_str)
    except ValueError:
      self.logger.debug('Invalid JSON %s. Not expiring key.', json_str)
      return False

    if 'expireOn' not in json_obj:
      self.logger.debug('No expiration timestamp. Not expiring key.')
      return False

    expire_str = json_obj['expireOn']
    format_str = '%Y-%m-%dT%H:%M:%S+0000'
    try:
      expire_time = datetime.datetime.strptime(expire_str, format_str)
    except ValueError:
      self.logger.warning(
          'Expiration timestamp "%s" not in format %s. Not expiring key.',
          expire_str, format_str)
      return False

    # Expire the key if and only if we have exceeded the expiration timestamp.
    return datetime.datetime.utcnow() > expire_time

  def _ParseAccountsData(self, account_data):
    """Parse the SSH key data into a user map.

    Args:
      account_data: string, the metadata server SSH key attributes data.

    Returns:
      dict, a mapping of the form: {'username': ['sshkey1, 'sshkey2', ...]}.
    """
    if not account_data:
      return {}
    lines = [line for line in account_data.splitlines() if line]
    user_map = {}
    for line in lines:
      split_line = line.split(':', 1)
      if len(split_line) != 2:
        self.logger.info('SSH key is not a complete entry: %s.', split_line)
        continue
      user, key = split_line
      if self._HasExpired(key):
        self.logger.debug('Expired SSH key for user %s: %s.', user, key)
        continue
      if user not in user_map:
        user_map[user] = []
      user_map[user].append(key)
    logging.debug('User accounts: %s.', user_map)
    return user_map

  def _GetAccountsData(self, metadata_dict):
    """Get the user accounts specified in metadata server contents.

    Args:
      metadata_dict: json, the deserialized contents of the metadata server.

    Returns:
      dict, a mapping of the form: {'username': ['sshkey1, 'sshkey2', ...]}.
    """
    metadata_dict = metadata_dict or {}

    try:
      instance_data = metadata_dict['instance']['attributes']
    except KeyError:
      instance_data = {}
      self.logger.warning('Instance attributes were not found.')

    try:
      project_data = metadata_dict['project']['attributes']
    except KeyError:
      project_data = {}
      self.logger.warning('Project attributes were not found.')
    valid_keys = [instance_data.get('sshKeys'), instance_data.get('ssh-keys')]
    block_project = instance_data.get('block-project-ssh-keys', '').lower()
    if block_project != 'true' and not instance_data.get('sshKeys'):
      valid_keys.append(project_data.get('ssh-keys'))
      valid_keys.append(project_data.get('sshKeys'))
    accounts_data = '\n'.join([key for key in valid_keys if key])
    return self._ParseAccountsData(accounts_data)

  def _UpdateUsers(self, update_users):
    """Provision and update Linux user accounts based on account metadata.

    Args:
      update_users: dict, authorized users mapped to their public SSH keys.
    """
    for user, ssh_keys in update_users.items():
      if not user or user in self.invalid_users:
        continue
      if not self.utils.UpdateUser(user, ssh_keys):
        self.invalid_users.add(user)

  def _RemoveUsers(self, remove_users):
    """Deprovision Linux user accounts that do not appear in account metadata.

    Args:
      remove_users: list, the username strings of the Linux accounts to remove.
    """
    for username in remove_users:
      self.utils.RemoveUser(username)
    self.invalid_users -= set(remove_users)

  def HandleAccounts(self, result):
    """Called when there are changes to the contents of the metadata server.

    Args:
      result: json, the deserialized contents of the metadata server.
    """
    self.logger.debug('Checking for changes to user accounts.')
    configured_users = self.utils.GetConfiguredUsers()
    desired_users = self._GetAccountsData(result)
    remove_users = sorted(set(configured_users) - set(desired_users.keys()))
    self._UpdateUsers(desired_users)
    self._RemoveUsers(remove_users)
    self.utils.SetConfiguredUsers(desired_users.keys())


def main():
  parser = optparse.OptionParser()
  parser.add_option('-d', '--debug', action='store_true', dest='debug',
                    help='print debug output to the console.')
  (options, _) = parser.parse_args()
  instance_config = config_manager.ConfigManager()
  if instance_config.GetOptionBool('Daemons', 'accounts_daemon'):
    AccountsDaemon(
        groups=instance_config.GetOptionString('Accounts', 'groups'),
        remove=instance_config.GetOptionBool('Accounts', 'deprovision_remove'),
        debug=bool(options.debug))


if __name__ == '__main__':
  main()
