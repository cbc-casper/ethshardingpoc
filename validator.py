from blocks import Block, Message
from blocks import MessagesLog
from blocks import MessagesLog
from config import SHARD_IDS
from config import VALIDATOR_NAMES
from config import VALIDATOR_WEIGHTS
from config import TTL_CONSTANT
from evm_transition import apply_to_state

from fork_choice import fork_choice, sharded_fork_choice

import copy

class UnresolvedDeps(Exception):
    pass


class ConsensusMessage:
    def __init__(self, block, name, justification=[]):
        self.estimate = block
        self.sender = name
        self.justification = justification

        assert isinstance(self.estimate, Block), "expected block"
        assert self.estimate.is_valid(), "expected block to be valid"

        assert self.sender in VALIDATOR_NAMES

        self.height = 0
        max_height = 0
        for m in self.justification:
            assert isinstance(m, ConsensusMessage), "expected justification to contain consensus messages"
            if m.height > max_height:
                if m.estimate.shard_ID == self.estimate.shard_ID:
                    max_height = m.height

        self.height = max_height + 1


class Validator:
    def __init__(self, name):
        assert name in VALIDATOR_NAMES, "expected a validator name"
        self.name = name
        self.consensus_messages = []

    def receive_consensus_message(self, message):
        for m in message.justification:
            assert isinstance(m, ConsensusMessage), "expected consensus message"
            if m not in self.consensus_messages:
                raise UnresolvedDeps

        self.consensus_messages.append(message)

    # assumes no equivocations exist
    def latest_messages(self):
        max_heights = dict.fromkeys(VALIDATOR_NAMES)
        L_M = dict.fromkeys(VALIDATOR_NAMES)
        for v in VALIDATOR_NAMES:
            max_heights[v] = -1

        for m in self.consensus_messages:
            if m.height > max_heights[m.sender]:
                max_heights[m.sender] = m.height
                L_M[m.sender] = m

        return L_M

    def get_weighted_blocks(self):
        weighted_blocks = {}
        L_M = self.latest_messages()
        for v in VALIDATOR_NAMES:
            if L_M[v] is not None:
                if L_M[v].estimate in weighted_blocks.keys():
                    weighted_blocks[L_M[v].estimate] += VALIDATOR_WEIGHTS[v]
                else:
                    weighted_blocks[L_M[v].estimate] = VALIDATOR_WEIGHTS[v]

        return weighted_blocks

    def get_blocks_from_consensus_messages(self):
        blocks = []
        for m in self.consensus_messages:
            blocks.append(m.estimate)
        return blocks

    # TODO: memoize? this shouldn't change
    def genesis_blocks(self):
        genesis_blocks = {}
        for m in self.consensus_messages:
            if m.sender == 0:
                genesis_blocks[m.estimate.shard_ID] = m.estimate
        return genesis_blocks


    def make_fork_choice(self, shard_ID):
        # the blocks in the view are the genesis blocks and blocks from consensus messages
        blocks = self.get_blocks_from_consensus_messages()
        weighted_blocks = self.get_weighted_blocks()
        genesis_blocks = self.genesis_blocks()

        # The root shard doesn't have filtered blocks
        for g in genesis_blocks.values():
            if g.parent_ID is None:
                root_choice = fork_choice(genesis_blocks[g.shard_ID], blocks, weighted_blocks)
                break

        # If we're just asking for the root shard, then we're done
        if root_choice.shard_ID == shard_ID:
            return root_choice

        # Getting sequence of shards from shard_ID to root shard
        backwards_shard_sequence = []
        this_ID = shard_ID
        while(this_ID != root_choice.shard_ID):
            backwards_shard_sequence.append(this_ID)
            this_ID = genesis_blocks[this_ID].parent_ID

        shard_sequence = []
        for i in range(len(backwards_shard_sequence)):
            shard_sequence.append(backwards_shard_sequence[len(backwards_shard_sequence) - 1 - i])

        for i in range(len(backwards_shard_sequence) - 1):
            assert shard_sequence[i] == genesis_blocks[shard_sequence[i+1]].parent_ID, "expected chain of parents!"


        # FORK CHOICE HAPPENS HERE:
        next_fork_choice = root_choice
        for i in range(len(backwards_shard_sequence)):
            next_fork_choice = sharded_fork_choice(shard_sequence[i], genesis_blocks, blocks, weighted_blocks, next_fork_choice)

        print("shard_sequence", shard_sequence)
        print("shard_ID", next_fork_choice.shard_ID, shard_ID)
        assert next_fork_choice.shard_ID == shard_ID, "expected fork choice to be on requested shard"

        return next_fork_choice

    def make_all_fork_choices(self):

        fork_choices = {}
        for shard_ID in SHARD_IDS:
            fork_choices[shard_ID] = self.make_fork_choice(shard_ID)
        return fork_choices

    def next_hop(self, block, target_shard_ID):
        if block.shard_ID == target_shard_ID:
            return block.shard_ID

        ret = None
        for neighbor_shard_ID in block.child_IDs:
            assert neighbor_shard_ID != block.shard_ID
            candidate = self.next_hop(block.sources[neighbor_shard_ID], target_shard_ID)
            if candidate is not None:
                assert ret is None
                ret = neighbor_shard_ID
                # break # <-- uncommenting would maintain correctness, but would disable asserting if multiple paths lead to the target_shard_ID

        return ret

    def make_block(self, shard_ID, mempools, drain_amount, genesis_blocks, TTL=TTL_CONSTANT):
        genesis_blocks = self.genesis_blocks()
        # RUN FORK CHOICE RULE ON SELF
        # will only have fork choices for parent and children
        my_fork_choice = self.make_fork_choice(shard_ID)
        # --------------------------------------------------------------------#


        # GET PREVBLOCK POINTER FROM FORK CHOICE
        prevblock = my_fork_choice
        # --------------------------------------------------------------------#


        # EXTEND THE TRANSACTION LOG FROM THE MEMPOOL
        prev_txn_log = prevblock.txn_log
        new_txn_log = copy.copy(prev_txn_log)
        data = []
        num_prev_txs = len(prev_txn_log)
        for i in range(drain_amount):
            if num_prev_txs + i < len(mempools[shard_ID]):
                new_tx = mempools[shard_ID][num_prev_txs + i]
                new_txn_log.append(new_tx)
                data.append(new_tx)
        # --------------------------------------------------------------------#


        neighbor_shard_IDs = []
        if my_fork_choice.parent_ID is not None:
            neighbor_shard_IDs.append(my_fork_choice.parent_ID)
        for IDs in my_fork_choice.child_IDs:
            neighbor_shard_IDs.append(IDs)


        # BUILD RECEIVED LOG WITH:
        received_log = MessagesLog()
        sources = {ID : genesis_blocks[ID] for ID in SHARD_IDS}
        for ID in neighbor_shard_IDs:
            if ID == shard_ID:
                continue

            neighbor_fork_choice = self.make_fork_choice(ID)
            # SOURCES = FORK CHOICE (except for self)
            sources[ID] = neighbor_fork_choice
            # RECEIVED = SENT MESSAGES FROM FORK CHOICE
            received_log.log[ID] = neighbor_fork_choice.sent_log.log[shard_ID]
        # --------------------------------------------------------------------#


        # PREP NEWLY RECEIVED PMESSAGES IN A RECEIVEDLOG FOR EVM:
        newly_received_messages = {}
        new_sent_messages = MessagesLog()
        for ID in neighbor_shard_IDs:
            previous_received_log_size = len(prevblock.received_log.log[ID])
            current_received_log_size = len(received_log.log[ID])
            newly_received_messages[ID] = received_log.log[ID][previous_received_log_size:]

        newly_received_payloads = MessagesLog()
        for ID in neighbor_shard_IDs:
            for m in newly_received_messages[ID]:
                if m.target_shard_ID == shard_ID:
                    newly_received_payloads.add_message(ID, m)

                else:
                    next_hop_ID = self.next_hop(prevblock, m.target_shard_ID)
                    if next_hop_ID is not None:
                        assert next_hop_ID in prevblock.child_IDs
                    else:
                        next_hop_ID = prevblock.parent_ID
                    new_sent_messages.log[next_hop_ID].append(Message(self.make_fork_choice(next_hop_ID), m.TTL, m.target_shard_ID, m.payload))

        # --------------------------------------------------------------------#


        # KEY EVM INTEGRATION HERE

        # this is where we have this function that produces the new vm state and the new outgoing payloads
        # new_vm_state, new_outgoing_payloads = apply_to_state(prevblock.vm_state, data, newly_received_payloads)

        new_vm_state, new_outgoing_payloads = apply_to_state(prevblock.vm_state, data, newly_received_payloads, genesis_blocks)

        # --------------------------------------------------------------------#


        # BUILD SENT LOG FROM NEW OUTGOING PAYLOADS
        # by this time new_sent_messages might already have some messages from rerouting above
        for ID in SHARD_IDS:
            if ID != shard_ID:
                for m in new_outgoing_payloads.log[ID]:
                    # if TTL == 0, then we'll make an invalid block
                    # one that sends a message that must be included by the base
                    # which already exists and therefore cannot include this message
                    if TTL > 0:
                        first_hop_ID = self.next_hop(prevblock, ID)
                        if first_hop_ID is not None:
                            assert first_hop_ID in [prevblock.parent_ID] + prevblock.child_IDs
                        else:
                            first_hop_ID = prevblock.parent_ID
                        new_sent_messages.log[first_hop_ID].append(Message(self.make_fork_choice(first_hop_ID), TTL, ID, m.payload))
                    else:
                        print("Warning: Not sending message because TTL == 0")

        sent_log = prevblock.sent_log.append_MessagesLog(new_sent_messages)
        # --------------------------------------------------------------------#




        return Block(shard_ID, prevblock, new_txn_log, sent_log, received_log, sources, new_vm_state)

    def make_new_consensus_message(self, shard_ID, mempools, drain_amount, genesis_blocks, TTL=TTL_CONSTANT):

        assert shard_ID in SHARD_IDS, "expected shard ID"
        assert isinstance(drain_amount, int), "expected int"
        assert isinstance(TTL, int), "expected int"
        assert isinstance(mempools, dict), "expected dict"
        new_block = self.make_block(shard_ID, mempools, drain_amount, genesis_blocks, TTL)
        new_message = ConsensusMessage(new_block, self.name, copy.copy(self.consensus_messages))
        self.receive_consensus_message(new_message)
        return new_message
