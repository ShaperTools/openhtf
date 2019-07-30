# Copyright 2015 Google Inc. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""User input module for OpenHTF.

Provides a plug which can be used to prompt the user for input. The prompt can
be displayed in the console, the OpenHTF web GUI, and custom frontends.
"""

from __future__ import print_function

import collections
import functools
import locale
import logging
import os
import platform
import select
import sys
import threading
import time
import uuid

from openhtf import PhaseOptions
from openhtf import plugs
from openhtf import util
from openhtf.util import console_output
from six.moves import input

if platform.system() != 'Windows':
  import termios

_LOG = logging.getLogger(__name__)

PROMPT = ''#''--> '


class PromptInputError(Exception):
  """Raised in the event that a prompt returns without setting the response."""


class MultiplePromptsError(Exception):
  """Raised if a prompt is invoked while there is an existing prompt."""


class PromptUnansweredError(Exception):
  """Raised when a prompt times out or otherwise comes back unanswered."""

class OperatorAttendanceViolation(Exception):
  """Raised when misuse of operator attendance marking is detected."""


Prompt = collections.namedtuple('Prompt', 'id message text_input')


class ConsolePrompt(threading.Thread):
  """Thread that displays a prompt to the console and waits for a response."""

  def __init__(self, message, callback, color=''):
    """Initializes a ConsolePrompt.

    Args:
      message: A string to be presented to the user.
      callback: A function to be called with the response string.
      color: An ANSI color code, or the empty string.
    """
    super(ConsolePrompt, self).__init__()
    self.daemon = True
    self._message = message
    self._callback = callback
    self._color = color
    self._stopped = False
    self._answered = False

  def Stop(self):
    """Mark this ConsolePrompt as stopped."""
    self._stopped = True
    if not self._answered:
      _LOG.debug('Stopping ConsolePrompt--prompt was answered from elsewhere.')

  def run(self):
    """Main logic for this thread to execute."""
    try:
      if platform.system() == 'Windows':
        # Windows doesn't support file-like objects for select(), so fall back
        # to raw_input().
        response = input(''.join((self._message,
                                  os.linesep,
                                  PROMPT)))
        self._answered = True
        self._callback(response)
      else:
        # First, display the prompt to the console.
        console_output.cli_print(self._message, color=self._color,
                                 end=os.linesep, logger=None)
        console_output.cli_print(PROMPT, color=self._color, end='', logger=None)
        sys.stdout.flush()

        # Before reading, clear any lingering buffered terminal input.
        if sys.stdin.isatty():
          termios.tcflush(sys.stdin, termios.TCIFLUSH)

        # Although this isn't threadsafe with do_setlocale=True, it doesn't work without it.
        encoding = locale.getpreferredencoding(do_setlocale=True)

        line = u''
        while not self._stopped:
          inputs, _, _ = select.select([sys.stdin], [], [], 0.001)
          for stream in inputs:
            if stream is sys.stdin:
              new = os.read(sys.stdin.fileno(), 1024)
              if not new:
                # Hit EOF!
                if not sys.stdin.isatty():
                  # We're running in the background somewhere, so the only way
                  # to respond to this prompt is the UI. Let's just wait for
                  # that to happen now. We'll give them a week :)
                  print("Waiting for a non-console response.")
                  time.sleep(60*60*24*7)
                else:
                  # They hit ^D (to insert EOF). Tell them to hit ^C if they
                  # want to actually quit.
                  print("Hit ^C (Ctrl+c) to exit.")
                  break
              line += new.decode(encoding)
              if '\n' in line:
                response = line[:line.find('\n')]
                self._answered = True
                self._callback(response)
              return
    finally:
      self._stopped = True


class UserInput(plugs.FrontendAwareBasePlug):
  """Get user input from inside test phases.

  Attributes:
    last_response: None, or a pair of (prompt_id, response) indicating the last
        user response that was received by the plug.
  """

  def __init__(self):
    super(UserInput, self).__init__()
    self.last_response = None
    self._prompt = None
    self._console_prompt = None
    self._response = None
    self._cond = threading.Condition()
    self._start_time_millis = None
    self._response_time_millis = None
    self._elapsed_seconds = 0
    self._attendance_log = []
    self._total_elapsed_seconds = 0
    self._track_operator_time = True
    # _LOG.debug("UserInput.__init__")

  def get_attendance_log(self):
    return self._attendance_log

  def get_total_elapsed_seconds(self):
    return self._total_elapsed_seconds

  def mark_operator_attendance_start(self):
    if not self._track_operator_time:
      return
    if  self._start_time_millis is not None:
      raise OperatorAttendanceViolation("Attempt to mark operator attendance start without marking an end to the previous one.")

    self._elapsed_seconds = 0
    self._response_time_millis = None
    self._start_time_millis = util.time_millis()

  def mark_operator_attendance_end(self):
    if not self._track_operator_time:
      return
    if self._start_time_millis is None:
      raise OperatorAttendanceViolation("Attempt ot mark operator attendance end without marking the start.")

    self._response_time_millis = util.time_millis()
    self._elapsed_seconds = (self._response_time_millis - self._start_time_millis) / float(1000)
    self._total_elapsed_seconds += self._elapsed_seconds
    self._attendance_log.append({
      'start_time_millis': self._start_time_millis,
      'elapsed_seconds': self._elapsed_seconds
    })

    _LOG.debug("Operator was required for %.1f seconds; %.1f minutes this test so far." %
                 (self._elapsed_seconds, self._total_elapsed_seconds / 60.0))

    self._start_time_millis = None # we use this to tag that we've completed an operator attendance cycle.

  def _asdict(self):
    """Return a dictionary representation of the current prompt."""
    with self._cond:
      if self._prompt is None:
        return
      return {'id': self._prompt.id,
              'message': self._prompt.message,
              'text-input': self._prompt.text_input}

  def _create_prompt(self, message, text_input):
    """Sets the prompt."""
    prompt_id = uuid.uuid4()
    _LOG.debug(u'Displaying prompt (%s): "%s"%s', prompt_id, message,
               ', Expects text' if text_input else '')

    self._response = None
    self._prompt = Prompt(id=prompt_id, message=message, text_input=text_input)
    self._console_prompt = ConsolePrompt(
        message, functools.partial(self.respond, prompt_id))

    self._console_prompt.start()
    self.notify_update()
    # _LOG.debug("UserInput._create_prompt")
    return prompt_id

  def remove_prompt(self):
    """Remove the prompt."""
    self._prompt = None
    self._console_prompt.Stop()
    self._console_prompt = None
    self.notify_update()
    # _LOG.debug("UserInput._remove_prompt")

  def prompt(self, message, text_input=False, timeout_s=None, cli_color='', track_operator_time=True):
    """Display a prompt and wait for a response.

    Args:
      message: A string to be presented to the user.
      text_input: A boolean indicating whether the user must respond with text.
      timeout_s: Seconds to wait before raising a PromptUnansweredError.
      cli_color: An ANSI color code, or the empty string.
      track_operator_time: If True, will time how long the prompt takes to answer.

    Returns:
      A string response, or the empty string if text_input was False.

    Raises:
      MultiplePromptsError: There was already an existing prompt.
      PromptUnansweredError: Timed out waiting for the user to respond.
    """
    self._track_operator_time = track_operator_time
    self.start_prompt(message, text_input, cli_color, track_operator_time)
    return self.wait_for_prompt(timeout_s)

  def start_prompt(self, message, text_input=False, cli_color='', track_operator_time=True):
    """Display a prompt.

    Args:
      message: A string to be presented to the user.
      text_input: A boolean indicating whether the user must respond with text.
      cli_color: An ANSI color code, or the empty string.

    Raises:
      MultiplePromptsError: There was already an existing prompt.

    Returns:
      A string uniquely identifying the prompt.
    """
    self._track_operator_time = track_operator_time
    with self._cond:
      if self._prompt:
        raise MultiplePromptsError
      prompt_id = uuid.uuid4().hex
      _LOG.debug('Displaying prompt (%s): "%s"%s', prompt_id, message,
                 ', Expects text input.' if text_input else '')

      self._response = None
      self._prompt = Prompt(
          id=prompt_id, message=message, text_input=text_input)
      self._console_prompt = ConsolePrompt(
          message, functools.partial(self.respond, prompt_id), cli_color)

      self.mark_operator_attendance_start()
      self._console_prompt.start()
      self.notify_update()
      # _LOG.debug("UserInput.start_prompt")
      return prompt_id

  def wait_for_prompt(self, timeout_s=None):
    """Wait for the user to respond to the current prompt.

    Args:
      timeout_s: Seconds to wait before raising a PromptUnansweredError.

    Returns:
      A string response, or the empty string if text_input was False.

    Raises:
      PromptUnansweredError: Timed out waiting for the user to respond.
    """
    with self._cond:
      # _LOG.debug("UserInput.wait_for_prompt")
      if self._prompt:
        if timeout_s is None:
          self._cond.wait(3600 * 24 * 365)
        else:
          self._cond.wait(timeout_s)
      if self._response is None:
        self.mark_operator_attendance_end()
        raise PromptUnansweredError
      return self._response

  def respond(self, prompt_id, response):
    """Respond to the prompt with the given ID.

    If there is no active prompt or the given ID doesn't match the active
    prompt, do nothing.

    Args:
      prompt_id: A string uniquely identifying the prompt.
      response: A string response to the given prompt.

    Returns:
      True if the prompt with the given ID was active, otherwise False.
    """
    _LOG.debug(u'Responding to prompt (%s): "%s"', prompt_id, response)
    with self._cond:
      if not (self._prompt and self._prompt.id == prompt_id):
        return False
      self._response = response
      self.last_response = (prompt_id, response)
      self.remove_prompt()
      self._cond.notifyAll()
      # _LOG.debug("UserInput.respond")
      self.mark_operator_attendance_end()
    return True


def prompt_for_test_start(
    message='Enter a DUT ID in order to start the test.', timeout_s=60*60*24,
    validator=lambda sn: sn, cli_color=''):
  """Return an OpenHTF phase for use as a prompt-based start trigger.

  Args:
    message: The message to display to the user.
    timeout_s: Seconds to wait before raising a PromptUnansweredError.
    validator: Function used to validate or modify the serial number.
    cli_color: An ANSI color code, or the empty string.
  """

  @PhaseOptions(timeout_s=timeout_s)
  @plugs.plug(prompts=UserInput)
  def trigger_phase(test, prompts):
    """Test start trigger that prompts the user for a DUT ID."""
    dut_id = prompts.prompt(
        message, text_input=True, timeout_s=timeout_s, cli_color=cli_color)
    test.test_record.dut_id = validator(dut_id)

  return trigger_phase
