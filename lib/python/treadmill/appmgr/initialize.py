"""Manages Treadmill node environment initialization."""
from __future__ import absolute_import

import logging

from ..osmodules import nodeinit


_LOGGER = logging.getLogger(__name__)


def initialize(tm_env):
    """One time initialization of the Treadmill environment."""
    _LOGGER.info('Initializing once.')

    nodeinit.initialize(tm_env)
