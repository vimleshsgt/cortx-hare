# Copyright (c) 2020 Seagate Technology LLC and/or its Affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# For any questions about this software or licensing,
# please email opensource@seagate.com or cortx-questions@seagate.com.
#

import logging
from collections import deque
from dataclasses import dataclass
from threading import Condition
from typing import Callable, Deque, Dict, Optional, Set, Tuple, Type

from hax.log import TRACE
from hax.message import (AnyEntrypointRequest, BaseMessage, BroadcastHAStates,
                         Die, HaNvecGetEvent, HaNvecSetEvent, ProcessEvent,
                         ProcessHaEvent, SnsOperation)
from hax.motr.util import LinkedList
from hax.types import Fid

LOG = logging.getLogger('hax')
MAX_GROUP_ID = 100000

__all__ = ['WorkPlanner']


@dataclass
class CommandMeta:
    # A fid value related to a command
    fid: Fid


@dataclass
class State:
    #
    # Group being executed currently
    current_group_id: int
    #
    # Group that is being populated by next add_command() invocation
    next_group_id: int
    #
    # Commands that are being processed now
    active_commands: LinkedList[BaseMessage]
    #
    # Mapping of id(active_command) -> CommandMeta
    # the idea is that any command in active_commands collection can
    # potentially have a metadata object stored in this Dict
    active_meta: Dict[int, CommandMeta]
    #
    # Types of commands that have already been added to group number
    # next_group_id.
    #
    # The idea is that WorkPlanner works like a queue: worker threads take the
    # commands from current_group_id, while the newly added commands are put
    # to the tail - to next_group_id. Sometimes WorkPlanner needs to increment
    # next_group_id, in order to make such a decision WorkPlanner must consider
    # the command type being added and check which command types are already
    # there in the next_group_id group. If there is no logical conflict, the
    # next_group_id remains the same, otherwise a new group is formed.
    next_group_commands: Set[Type[BaseMessage]]
    #
    # If WorkPlanner is in shutting down mode.
    # Shutting down mode is a special mode when WorkPlanner starts issuing
    # poison pills ('Die' commands) making sure that all the threads get such
    # commands ASAP.
    #
    # The flag is set to true by invoking WorkPlanner.shutdown() method.
    is_shutdown: bool


class WorkPlanner:
    '''
    Thread synchronizing block that is used as a work planner for Motr-aware
    threads (see ConsumerThread). This synchronizing primitive guarantees that

    1. The messages can be processed by an arbitrary number of ConsumerThread
       threads.

    2. The parallelism doesn't break semantics (some messages can be processed
       in parallel to others, while some other not; the ConsumerThread's don't
       need to worry about that).
    '''
    def __init__(self,
                 init_state_factory: Optional[Callable[[], State]] = None):
        fn = init_state_factory or self._create_initial_state

        self.state = fn()
        self.backlog: Deque[BaseMessage] = deque()
        self.asap_list: Deque[BaseMessage] = deque()
        self.b_lock = Condition()

    def is_empty(self) -> bool:
        '''Checks whether the backlog is empty. Blocking call.'''
        with self.b_lock:
            return not self.backlog

    def add_command(self, command: BaseMessage) -> None:
        '''Adds the given command to the execution plan. Blocking call.'''
        LOG.log(TRACE, '[WP]Before add_command: %s', command)
        with self.b_lock:
            cmd, is_asap = self._assign_group(command)
            LOG.log(TRACE, '[WP]Cmd %s is added. Current state: %s', cmd,
                    self.state)
            backlog = self.backlog
            if is_asap:
                backlog = self.asap_list
            backlog.append(cmd)
            # Some threads may be waiting because of an empty backlog - let's
            # notify them
            self.b_lock.notifyAll()

    def _create_initial_state(self) -> State:
        '''Default factory method that returns initial state.

           Invoked from WorkPlanner's __init__ method.
        '''
        return State(next_group_id=0,
                     active_commands=LinkedList(),
                     active_meta={},
                     current_group_id=0,
                     next_group_commands=set(),
                     is_shutdown=False)

    def _create_poison(self) -> BaseMessage:
        '''Creates poison pill - Die command. Used in a special 'shutting down'

           mode to stop worker threads gracefully.
        '''

        cmd = Die()
        # Since it is a special case, no _assign_group() will be invoked to
        # find the proper group. In fact, we want to stop the threads as soon
        # as possible, that's why it doesn't make sense to postpone the
        # command. In other words, it must belong to the group which is
        # currently active.
        cmd.group = self.state.current_group_id
        return cmd

    def get_next_command(self) -> BaseMessage:
        '''
        Returns the command that the worker thread can start executing just
        right away.

        The function will block the invoking thread either if there are no
        commands (backlog is empty) or the message belongs to a group from
        the future. The invoking thread will be unblocked automatically
        when the command becomes eligible.
        '''
        def next_cmd() -> Optional[BaseMessage]:
            if self.state.is_shutdown:
                return self._create_poison()

            for backlog, is_allowed in [(self.asap_list,
                                         self._is_allowed_asap),
                                        (self.backlog, self._is_allowed)]:
                if backlog:
                    cmd = backlog.popleft()
                    if is_allowed(cmd):
                        self._add_active_cmd(cmd)
                        LOG.log(TRACE, '[WP]Cmd %s taken!', cmd)
                        return cmd
                    else:
                        # Given the command is not eligble, put it back
                        # to the same place it has been taken from.
                        backlog.appendleft(cmd)
            return None

        while True:
            LOG.log(TRACE, '[WP]Trying to get new command')
            with self.b_lock:
                cmd = next_cmd()
                if cmd:
                    return cmd
                LOG.log(
                    TRACE,
                    '[WP]Blocking thread: no eligible commands in backlog')
                self.b_lock.wait()

    def shutdown(self):
        '''Put the WorkPlanner to 'shutting down' mode. After this function is
        invoked WorkPlanner will issue Die commands only.'''

        with self.b_lock:
            LOG.debug('WorkPlanner is shutting down')
            self.state.is_shutdown = True
            self.b_lock.notifyAll()

    def _remove_active_cmd(self, command: BaseMessage) -> None:
        self.state.active_commands.remove(command)
        key = id(command)
        if key in self.state.active_meta:
            del self.state.active_meta[key]

    def _add_active_cmd(self, command: BaseMessage) -> None:
        self.state.active_commands.add(command)
        meta = self._extract_meta(command)
        if meta:
            key = id(command)
            self.state.active_meta[key] = meta

    def _extract_meta(self, command: BaseMessage) -> Optional[CommandMeta]:
        if isinstance(command, ProcessEvent):
            return CommandMeta(fid=command.evt.fid)
        return None

    def _is_allowed_asap(self, command: BaseMessage) -> bool:
        meta = self._extract_meta(command)
        if not meta:
            return True

        for k, v in self.state.active_meta.items():
            # TODO in the future `==` will have to be replaced with something
            # more command-specific.
            # E.g. two ProcessEvents conflict if their metas contain the same
            # fid.
            #
            # At this moment we support CommandMeta for ProcessEvent, so for
            # now this 'is_conflict' logic is written like this:
            if v.fid == meta.fid:
                return False
        return True

    def _is_allowed(self, command: BaseMessage) -> bool:
        '''
        Returns True group_id equal to the currently active group

        The command is allowed for execution if and only if the command has
        group_id equal to the currently active group (see
        State.current_group_id). The command group ids are assigned just once
        when the command is being added to the WorkPlanner via add_command()
        method.
        '''
        def is_current(cmd: BaseMessage, st: State) -> bool:
            return cmd.group == st.current_group_id

        with self.b_lock:
            state = self.state
            return is_current(command, state)

    def _get_increased_group(self, current: int) -> int:
        ''' Returns the next valid group_id number by the given current value.

            Performs no side effects.
        '''
        new_value = current + 1
        # In Python, every int uses an arbitrary-precision maths. In other
        # words, if an int value becomes greater than 4 bytes can store, no
        # overflow will happen. Instead, the variable will use more and more
        # additional chunks of memory. That's why group id should be wrapped
        # back to zero manually.
        if new_value > MAX_GROUP_ID:
            new_value = 0
        return new_value

    def _inc_group(self):
        '''Increases the currently active group.

        The method is invoked by WorkPlanner when
        all the commands from the current group have
        already been processed.

        Assumes that b_lock is acquired already.
        '''
        state = self.state
        cur_group_id = state.current_group_id
        change_next_group = state.next_group_id == cur_group_id

        state.current_group_id = self._get_increased_group(cur_group_id)

        if change_next_group:
            state.next_group_id = state.current_group_id
            state.next_group_commands = set()

    def notify_finished(self, command: BaseMessage) -> None:
        """Method invoked by the worker thread when the command is executed.

        The method must be invoked by the worker thread when the command
        is executed.
        """

        with self.b_lock:
            state = self.state
            self._remove_active_cmd(command)
            LOG.log(TRACE, '[WP]Cmd %s removed. Current state: %s', command,
                    state)

            if state.active_commands:
                return
            for c in self.backlog:
                if c.group == state.current_group_id:
                    return
            # if we're here, command was the only one belonging to group
            self._inc_group()
            LOG.log(TRACE, '[WP]Active group changed to %s',
                    state.current_group_id)
            # The group changed, let's unblock those who are waiting for
            # this group
            self.b_lock.notifyAll()

    def _should_increase_group(self, cmd: BaseMessage) -> bool:
        """Predicate function.

        Returns True if and only if cmd command CANNOT
        be added to group number next_group_id.
        Assumes that b_lock is acquired already.
        """
        def has(cmd_type: Type[BaseMessage]) -> bool:
            ''' Checks if the group being currently formed (i.e. next_group)
                contains a message of the given type.
            '''
            return cmd_type in self.state.next_group_commands

        if not self.state.next_group_commands:
            # current group is empty -> join it freely
            return False
        if isinstance(cmd, ProcessEvent):
            return False
        if isinstance(cmd, HaNvecGetEvent):
            # HaNvecGetEvent can be done in parallel to any other commands.
            # No need to form the new group for it.
            return False
        if isinstance(cmd, HaNvecSetEvent):
            # HaNvecSetEvent can be done in parallel to any other commands.
            # No need to form the new group for it.
            return False
        if isinstance(cmd, AnyEntrypointRequest):
            # Entrypoint requests can be processed in parallel to other
            # requests are they are per processes. In a situation where
            # if an entrypoint request needs to block on some other request
            # e.g. BroadcastHAStates, then the wait needs to explicit.
            # For example, see FirstEntrypoint request code in hax/handler.py.
            return False
        if isinstance(cmd, ProcessHaEvent):
            return True
        if isinstance(cmd, BroadcastHAStates):
            return True

        if isinstance(cmd, SnsOperation):
            # Start new group if there is another SNS operation within the
            # current group.
            return has(SnsOperation)
        return False

    def _assign_group(self, cmd: BaseMessage) -> Tuple[BaseMessage, bool]:
        ''' Sets the correct group_id to the command. Side effect: updates
        self.state.

        Returns Tuple with the updated command and a boolean flag saying
        whether this command must be added out of order (is_asap).

        Must be invoked with b_lock acquired. The method is invoked from
        add_command(cmd).

        The given command gets either state.next_group_id value or
        (state.next_group_id+1); in the latter case state is updated and we
        can say that command cmd starts a new group. Effectively it looks
        like this:
        state.next_group_id := (state.next_group_id + 1) mod MAX_GROUP_ID.

        Command cmd starts a new group if:
        1. cmd is BroadcastHAStates
        2. cmd is an SNS operation AND next_group has SNS operation already
        Otherwise command will join existing next_group.

        '''
        def join_group(cmd: BaseMessage) -> BaseMessage:
            cmd.group = self.state.next_group_id
            self.state.next_group_commands.add(type(cmd))
            return cmd

        def next_group() -> None:
            self.state.next_group_commands = set()
            self.state.next_group_id = self._get_increased_group(
                self.state.next_group_id)

        if (isinstance(cmd, AnyEntrypointRequest)
                or isinstance(cmd, HaNvecGetEvent)
                or isinstance(cmd, HaNvecSetEvent)
                or isinstance(cmd, ProcessEvent)):
            # Entrypoint and Die will always be added to the CURRENT group
            # (the one being currently active), so they can be executed at
            # first priority.
            #
            # Entrypoint is also a special case: it should not be delayed and
            # we also know that they appear during bootstrap when a huge flow
            # of BroadcastHAStates events appear. We just don't want to block
            # because of them.
            cmd.group = self.state.current_group_id
            return (cmd, True)
        elif isinstance(cmd, Die):
            # Normally, no one needs to add the Die command to the backlog
            # except for the unit tests.
            #
            # shutdown() mechanism generates Die command dynamically anyway.
            return (join_group(cmd), False)

        if self._should_increase_group(cmd):
            next_group()

        return (join_group(cmd), False)
