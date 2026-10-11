"""Common interface for the Teams computer-use backends.

Both backends (UI Automation and screenshot+vision) implement the same three
operations so bridge.py does not care which one is active:

    focus_self_chat()      -> bring Teams to front and VERIFY it shows the self chat
    read_new_messages()    -> messages that appeared since the last read (deduped)
    send_message(text)     -> paste text into the compose box and press Enter

Backends only have to implement read_visible_messages(); dedupe lives in
state.py so it can be unit tested on Linux.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import List, Optional


class ChatGuardError(RuntimeError):
    """Raised when the visible window/chat is not the configured self chat.

    The bridge aborts the whole cycle when this happens: nothing is read and
    nothing is sent.
    """


class BackendError(RuntimeError):
    """Raised when the UI could not be read or driven (window missing etc.)."""


@dataclass
class ChatMessage:
    text: str
    timestamp: str = ""          # whatever the UI exposes; "" if unknown
    raw: str = ""                # raw accessible name / model output, for debugging
    fp: str = ""                 # base fingerprint (set by state.assign_fingerprints)
    sfp: str = ""                # version-INDEPENDENT fingerprint (baseline/seen-ever)
    occ_fp: str = ""             # fingerprint incl. occurrence index in this read
    extra: dict = field(default_factory=dict)


class ChatBackend(abc.ABC):
    """Interface shared by teams_uia.TeamsUIABackend and teams_vision.TeamsVisionBackend."""

    def __init__(self) -> None:
        self._state = None  # injected by bridge (state.StateStore)

    def attach_state(self, state) -> None:
        self._state = state

    @abc.abstractmethod
    def focus_self_chat(self) -> str:
        """Focus Teams and verify the self chat is open.

        Returns the title text that was verified. Raises ChatGuardError if the
        title does not match the configured pattern, BackendError if Teams is
        not found.
        """

    @abc.abstractmethod
    def read_visible_messages(self) -> List[ChatMessage]:
        """Return the messages currently visible in the chat, oldest first."""

    @abc.abstractmethod
    def send_message(self, text: str) -> None:
        """Type text into the self chat compose box and send it.

        Implementations MUST re-verify the chat title right before sending.
        """

    def read_new_messages(self) -> Optional[List[ChatMessage]]:
        """Visible messages filtered through the persistent dedupe state.

        Returns None when the state had to be (re)initialised and nothing
        should be executed this cycle.
        """
        if self._state is None:
            raise RuntimeError("attach_state() must be called first")
        return self._state.filter_new(self.read_visible_messages())

    def dump_debug(self, path: str) -> None:  # pragma: no cover - optional
        raise NotImplementedError
