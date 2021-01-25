#!/usr/bin/python3
# -*- coding: utf-8 -*-
# key-mapper - GUI for device specific keyboard mappings
# Copyright (C) 2021 sezanzeb <proxima@hip70890b.de>
#
# This file is part of key-mapper.
#
# key-mapper is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# key-mapper is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with key-mapper.  If not, see <https://www.gnu.org/licenses/>.


"""Inject a keycode based on the mapping."""


import itertools
import asyncio

from evdev.ecodes import EV_KEY, EV_ABS

from keymapper.logger import logger
from keymapper.mapping import DISABLE_CODE
from keymapper.dev import utils


# this state is shared by all KeycodeMappers of this process

# maps mouse buttons to macro instances that have been executed.
# They may still be running or already be done. Just like unreleased,
# this is a mapping of (type, code). The value is not included in the
# key, because a key release event with a value of 0 needs to be able
# to find the running macro. The downside is that a d-pad cannot
# execute two macros at once, one for each direction.
# Only sequentially.
active_macros = {}

# mapping of future release event (type, code) to an Unreleased object,
# All key-up events have a value of 0, so it is not added to
# the tuple. This is needed in order to release the correct event
# mapped on a D-Pad. Each direction on one D-Pad axis reports the
# same type and code, but different values. There cannot be both at
# the same time, as pressing one side of a D-Pad forces the other
# side to go up. If both sides of a D-Pad are mapped to different
# event-codes, this data structure helps to figure out which of those
# two to release on an event of value 0. Same goes for the Wheel.
# The input event is remembered to make sure no duplicate down-events
# are written. Since wheels report a lot of "down" events that don't
# serve any purpose when mapped to a key, those duplicate down events
# should be removed. If the same type and code arrives but with a
# different value (direction), there must be a way to check if the
# event is actually a duplicate and not a different event.
unreleased = {}


def is_key_down(value):
    """Is this event value a key press."""
    return value != 0


def is_key_up(value):
    """Is this event value a key release."""
    return value == 0


def write(uinput, key):
    """Shorthand to write stuff."""
    uinput.write(*key)
    uinput.syn()


COMBINATION_INCOMPLETE = 1  # not all keys of the combination are pressed
NOT_COMBINED = 2  # this key is not part of a combination


def subsets(combination):
    """Return a list of subsets of the combination.

    If combination is only one element long it returns an empty list,
    because it's not a combination and there is no reason to iterate.

    Includes the complete input as well.

    Parameters
    -----------
    combination : tuple
        tuple of 3-tuples, each being int, int, int (type, code, value)
    """
    combination = list(combination)
    lengths = list(range(2, len(combination) + 1))
    lengths.reverse()
    return list(itertools.chain.from_iterable(
        itertools.combinations(combination, length)
        for length in lengths
    ))


class Unreleased:
    """This represents a key that has been pressed but not released yet."""
    __slots__ = (
        'target_type_code',
        'input_event_tuple',
        'key'
    )

    def __init__(self, target_type_code, input_event_tuple, key):
        """
        Parameters
        ----------
        target_type_code : 2-tuple
            int type and int code of what was injected or forwarded
        input_event_tuple : 3-tuple
            the original event, int, int, int / type, code, value
        key : tuple of 3-tuples
            what was used to index key_to_code and macros when stuff
            was triggered
        """
        self.target_type_code = target_type_code
        self.input_event_tuple = input_event_tuple
        self.key = key

        if (
            not isinstance(input_event_tuple[0], int) or
            len(input_event_tuple) != 3
        ):
            raise ValueError(
                'Expected input_event_tuple to be a 3-tuple of ints, but '
                f'got {input_event_tuple}'
            )

        unreleased[input_event_tuple[:2]] = self

    def __str__(self):
        return (
            'Unreleased('
            f'target{self.target_type_code},'
            f'input{self.input_event_tuple},'
            f'key{"(None)" if self.key is None else self.key}'
            ')'
        )

    def __repr__(self):
        return self.__str__()


def find_by_event(key):
    """Find an unreleased entry by an event.

    If such an entry exists, it was created by an event that is exactly
    like the input parameter (except for the timestamp).

    That doesn't mean it triggered something, only that it was seen before.
    """
    unreleased_entry = unreleased.get(key[:2])
    if unreleased_entry and unreleased_entry.input_event_tuple == key:
        return unreleased_entry

    return None


def find_by_key(key):
    """Find an unreleased entry by a combination of keys.

    If such an entry exist, it was created when a combination of keys
    (which matches the parameter) (can also be of len 1 = single key)
    ended up triggering something.

    Parameters
    ----------
    key : tuple of 3-tuples
    """
    unreleased_entry = unreleased.get(key[-1][:2])
    if unreleased_entry and unreleased_entry.key == key:
        return unreleased_entry

    return None


def print_unreleased():
    """For debugging purposes."""
    logger.debug('unreleased:')
    logger.debug('\n'.join([
        f'    {key}: {str(value)}' for key, value in unreleased.items()
    ]))


class KeycodeMapper:
    def __init__(self, source, mapping, uinput, key_to_code, macros):
        """Create a keycode mapper for one virtual device.

        There may be multiple KeycodeMappers for one hardware device. They
        share some state (unreleased and active_macros) with each other.

        Parameters
        ----------
        source : InputDevice
            where events used in handle_keycode come from
        mapping : Mapping
            the mapping that is the source of key_to_code and macros,
            only used to query config values.
        uinput : UInput:
            where to inject events to
        key_to_code : dict
            mapping of ((type, code, value),) to linux-keycode
            or multiple of those like ((...), (...), ...) for combinations
            combinations need to be present in every possible valid ordering.
            e.g. shift + alt + a and alt + shift + a.
            This is needed to query keycodes more efficiently without having
            to search mapping each time.
        macros : dict
            mapping of ((type, code, value),) to _Macro objects.
            Combinations work similar as in key_to_code
        """
        self.source = source
        self.max_abs = utils.get_max_abs(source)
        self.mapping = mapping
        self.uinput = uinput

        # some type checking, prevents me from forgetting what that stuff
        # is supposed to be when writing tests.
        # TODO create that stuff (including macros) from mapping here instead
        #  of the injector
        for key in key_to_code:
            for sub_key in key:
                if abs(sub_key[2]) > 1:
                    raise ValueError(
                        f'Expected values to be one of -1, 0 or 1, '
                        f'but got {key}'
                    )

        self.key_to_code = key_to_code
        self.macros = macros

    def _get_key(self, key):
        """If the event triggers stuff, get the key for that.

        This key can be used to index `key_to_code` and `macros` and it might
        be a combination of keys.

        Otherwise, for unmapped events, returns the input.

        The return format is always a tuple of 3-tuples, each 3-tuple being
        type, code, value (int, int, int)

        Parameters
        ----------
        key : int, int, int
            3-tuple of type, code, value
            Value should be one of -1, 0 or 1
        """
        unreleased_entry = find_by_event(key)

        # The key used to index the mappings `key_to_code` and `macros`.
        # If the key triggers a combination, the returned key will be that one
        # instead
        value = key[2]
        key = (key,)

        if unreleased_entry is not None and unreleased_entry.key is not None:
            # seen before. If this key triggered a combination,
            # use the combination that was triggered by this as key.
            return unreleased_entry.key

        if is_key_down(value):
            # get the key/combination that the key-down would trigger

            # the triggering key-down has to be the last element in
            # combination, all others can have any arbitrary order. By
            # checking all unreleased keys, a + b + c takes priority over
            # b + c, if both mappings exist.
            # WARNING! the combination-down triggers, but a single key-up
            # releases. Do not check if key in macros and such, if it is an
            # up event. It's going to be False.
            combination = tuple([
                value.input_event_tuple for value
                in unreleased.values()
            ])
            if key[0] not in combination:  # might be a duplicate-down event
                combination += key

            # find any triggered combination. macros and key_to_code contain
            # every possible equivalent permutation of possible macros. The
            # last key in the combination needs to remain the newest key
            # though.
            for subset in subsets(combination):
                if subset[-1] != key[0]:
                    # only combinations that are completed and triggered by
                    # the newest input are of interest
                    continue

                if subset in self.macros or subset in self.key_to_code:
                    key = subset
                    break
            else:
                # no subset found, just use the key. all indices are tuples of
                # tuples, both for combinations and single keys.
                if value == 1 and len(combination) > 1:
                    logger.key_spam(combination, 'unknown combination')

        return key

    def handle_keycode(self, event, forward=True):
        """Write mapped keycodes, forward unmapped ones and manage macros.

        As long as the provided event is mapped it will handle it, it won't
        check any type, code or capability anymore. Otherwise it forwards
        it as it is.

        Parameters
        ----------
        event : evdev.InputEvent
        forward : bool
            if False, will not forward the event if it didn't trigger any
            mapping
        """
        if event.type == EV_KEY and event.value == 2:
            # button-hold event. Linux creates them on its own for the
            # injection-fake-device if the release event won't appear,
            # no need to forward or map them.
            return

        # normalize event numbers to one of -1, 0, +1. Otherwise
        # mapping trigger values that are between 1 and 255 is not
        # possible, because they might skip the 1 when pressed fast
        # enough.
        original_tuple = (event.type, event.code, event.value)
        event.value = utils.normalize_value(event, self.max_abs)

        # the tuple of the actual input event. Used to forward the event if
        # it is not mapped, and to index unreleased and active_macros. stays
        # constant
        event_tuple = (event.type, event.code, event.value)
        type_code = (event.type, event.code)
        active_macro = active_macros.get(type_code)

        key = self._get_key(event_tuple)
        is_mapped = key in self.macros or key in self.key_to_code

        """Releasing keys and macros"""

        if is_key_up(event.value):
            if active_macro is not None and active_macro.is_holding():
                # Tell the macro for that keycode that the key is released and
                # let it decide what to do with that information.
                active_macro.release_key()
                logger.key_spam(key, 'releasing macro')

            if type_code in unreleased:
                # figure out what this release event was for
                target_type, target_code = (
                    unreleased[type_code].target_type_code
                )
                del unreleased[type_code]

                if target_code == DISABLE_CODE:
                    logger.key_spam(key, 'releasing disabled key')
                elif target_code is None:
                    logger.key_spam(key, 'releasing key')
                elif type_code != (target_type, target_code):
                    # release what the input is mapped to
                    logger.key_spam(key, 'releasing %s', target_code)
                    write(self.uinput, (target_type, target_code, 0))
                elif forward:
                    # forward the release event
                    logger.key_spam((original_tuple,), 'forwarding release')
                    write(self.uinput, original_tuple)
                else:
                    logger.key_spam(key, 'not forwarding release')
            elif event.type != EV_ABS:
                # ABS events might be spammed like crazy every time the
                # position slightly changes
                logger.key_spam(key, 'unexpected key up')

            # everything that can be released is released now
            return

        """Filtering duplicate key downs"""

        if is_mapped and is_key_down(event.value):
            # unmapped keys should not be filtered here, they should just
            # be forwarded to populate unreleased and then be written.

            if find_by_key(key) is not None:
                # this key/combination triggered stuff before.
                # duplicate key-down. skip this event. Avoid writing millions
                # of key-down events when a continuous value is reported, for
                # example for gamepad triggers or mouse-wheel-side buttons
                logger.key_spam(key, 'duplicate key down')
                return

            # it would start a macro usually
            if key in self.macros and active_macro and active_macro.running:
                # for key-down events and running macros, don't do anything.
                # This avoids spawning a second macro while the first one is
                # not finished, especially since gamepad-triggers report a ton
                # of events with a positive value.
                logger.key_spam(key, 'macro already running')
                return

        """starting new macros or injecting new keys"""

        if is_key_down(event.value):
            # also enter this for unmapped keys, as they might end up
            # triggering a combination, so they should be remembered in
            # unreleased

            if key in self.macros:
                macro = self.macros[key]
                active_macros[type_code] = macro
                Unreleased((None, None), event_tuple, key)
                macro.press_key()
                logger.key_spam(key, 'maps to macro %s', macro.code)
                asyncio.ensure_future(macro.run())
                return

            if key in self.key_to_code:
                target_code = self.key_to_code[key]
                # remember the key that triggered this
                # (this combination or this single key)
                Unreleased((EV_KEY, target_code), event_tuple, key)

                if target_code == DISABLE_CODE:
                    logger.key_spam(key, 'disabled')
                    return

                logger.key_spam(key, 'maps to %s', target_code)
                write(self.uinput, (EV_KEY, target_code, 1))
                return

            if forward:
                logger.key_spam((original_tuple,), 'forwarding')
                write(self.uinput, original_tuple)
            else:
                logger.key_spam((event_tuple,), 'not forwarding')

            # unhandled events may still be important for triggering
            # combinations later, so remember them as well.
            Unreleased((event_tuple[:2]), event_tuple, None)
            return

        logger.error('%s unhandled', key)
