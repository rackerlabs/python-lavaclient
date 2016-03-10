# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Lava client exceptions"""


class LavaError(Exception):
    """Base class for all Lava client exceptions"""
    pass


class InvalidError(LavaError):
    pass


class AuthenticationError(LavaError):
    pass


class AuthorizationError(LavaError):
    pass


class RequestError(LavaError):
    """Represents a transient request error (e.g. timeout, host not found) or a
    good request that returned 4xx or 5xx"""

    def __init__(self, msg, code=None):
        super(RequestError, self).__init__(msg)
        self.code = code


class ApiError(LavaError):
    pass


class FailedError(LavaError):
    """The API request completed successfully, but the desired action on the
    server failed"""
    pass


class ConnectionError(LavaError):
    """The API could not be reached"""
    pass


class TimeoutError(LavaError):
    """The action timed out"""
    pass


class ProxyError(LavaError):
    """Error in SOCKS proxy over SSH"""
    pass
