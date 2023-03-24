# Copyright 2023 The DLRover Authors. All rights reserved.
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

import pickle
from datetime import datetime
from typing import Dict, Optional, Set, Tuple

import grpc
from torch.distributed import Store
from torch.distributed.elastic.rendezvous.api import (
    RendezvousConnectionError,
    RendezvousParameters,
)
from torch.distributed.elastic.rendezvous.dynamic_rendezvous import (
    RendezvousBackend,
    Token,
)

from dlrover.python.elastic_agent.master_client import (
    GlobalMasterClient,
    MasterClient,
)
from dlrover.python.elastic_agent.torch.master_kv_store import MasterKVStore


class _NodeDesc:
    """Describes a node in the rendezvous.

    Attributes:
        addr:
            The FQDN of the node or user specified local node address.
        pid:
            The id of the process in which the rendezvous handler runs.
        local_id:
            A process-wide unique id.
    """

    addr: str
    pid: int
    local_id: int

    def __repr__(self) -> str:
        return f"{self.addr}_{self.pid}_{self.local_id}"


class RendezvousState:
    """Holds the state of a rendezvous.

    Attributes:
        round:
            The current round of the rendezvous.
        complete:
            A boolean value indicating whether the current round of the
            rendezvous is complete.
        deadline:
            The time at which the current round of the rendezvous will be
            considered complete if it is still waiting for nodes to join.
        closed:
            A boolean value indicating whether the rendezvous is closed.
        participants:
            A dictionary of the participants and their corresponding ranks.
        wait_list:
            A set of nodes that are waiting to participate in the next round of
            the rendezvous.
        last_heartbeats:
            A dictionary containing each node's last heartbeat time.
    """

    round: int
    complete: bool
    deadline: Optional[datetime]
    closed: bool
    participants: Dict[_NodeDesc, int]
    wait_list: Set[_NodeDesc]
    last_heartbeats: Dict[_NodeDesc, datetime]

    def __init__(self) -> None:
        self.round = 0
        self.complete = False
        self.deadline = None
        self.closed = False
        self.participants = {}
        self.wait_list = set()
        self.last_heartbeats = {}


class DlroverRendezvousBackend(RendezvousBackend):
    """Represents an etcd-based rendezvous backend.

    Args:
        client:
            The ``master_client.MasterClient`` instance to use
            to communicate with the master server.
        run_id:
            The run id of the rendezvous.
    """

    _client: MasterClient
    _key: str

    def __init__(self, run_id: str, key_prefix) -> None:
        if not run_id:
            raise ValueError("The run id must be a non-empty string.")

        self._client = GlobalMasterClient.MASTER_CLIENT
        self._key = key_prefix + run_id

    @property
    def name(self) -> str:
        """See base class."""
        return "dlrover-master"

    def get_state(self) -> Optional[Tuple[bytes, Token]]:
        """See base class."""
        try:
            result = self._client.get_rdzv_state(self._key)
        except grpc.RpcError as exc:
            raise RendezvousConnectionError(
                "The connection to job master has failed."
                "See inner exception for details."
            ) from exc

        new_state_bits = result[0]
        token = result[1]
        if new_state_bits == pickle.dumps(""):
            return None
        rdzv_state = pickle.loads(new_state_bits)
        return new_state_bits, token

    def set_state(
        self, state: bytes, token: Optional[Token] = None
    ) -> Optional[Tuple[bytes, Token, bool]]:
        """See base class."""

        def get_state():
            result = self.get_state()
            if result is not None:
                tmp = *result, False
                return tmp
            return None

        if token:
            try:
                token = int(token)
            except ValueError:
                return get_state()
        else:
            token = 0
        try:
            rdzv_state = pickle.loads(state)
            participant_num = len(rdzv_state.participants)
            wait_num = len(rdzv_state.wait_list)
            succeed = self._client.set_rdzv_state(
                self._key,
                state,
                token,
                participant_num,
                wait_num,
            )

        except grpc.RpcError as exc:
            succeed = False
            raise RendezvousConnectionError(
                "The connection to job master has failed. "
                "See inner exception for details."
            ) from exc

        if not succeed:
            return get_state()

        return state, token, succeed


def create_backend(
    params: RendezvousParameters,
) -> Tuple[DlroverRendezvousBackend, Store]:
    """Creates a new :py:class:`DlroverRendezvousBackend` from the specified
    parameters.
    """

    backend = DlroverRendezvousBackend(
        params.run_id, key_prefix="torch.elastic.rendezvous."
    )

    store = MasterKVStore("/torch/elastic/store")

    return backend, store
