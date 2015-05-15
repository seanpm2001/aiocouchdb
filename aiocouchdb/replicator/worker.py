# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 Alexander Shorin
# All rights reserved.
#
# This software is licensed as described in the file LICENSE, which
# you should have received as part of this distribution.
#

import asyncio
import binascii
import logging
import os

from collections import defaultdict

from .abc import ISourcePeer, ITargetPeer
from .work_queue import WorkQueue


__all__ = (
    'ReplicationWorker',
)


log = logging.getLogger(__name__)


class ReplicationWorker(object):
    """Replication worker is a base unit that does all the hard work on transfer
    documents from Source peer the Target one.

    :param source: Source peer
    :param target: Target peer
    :param changes_queue: A queue from where new changes events will be fetched
    :param reports_queue: A queue to where worker will send all reports about
                          replication progress
    :param int batch_size: Amount of events to get from `changes_queue`
                           to process
    :param int max_conns: Amount of simultaneous connection to make against
                          peers at the same time
    """

    def __init__(self,
                 rep_id: str,
                 source: ISourcePeer,
                 target: ITargetPeer,
                 changes_queue: WorkQueue,
                 reports_queue: WorkQueue, *,
                 batch_size: int,
                 max_conns: int):
        self.source = source
        self.target = target
        self.changes_queue = changes_queue
        self.reports_queue = reports_queue
        self.batch_size = batch_size
        self.max_conns = max_conns
        self._id = binascii.hexlify(os.urandom(4)).decode()
        self._rep_id = rep_id

    @property
    def id(self) -> str:
        """Returns Worker ID."""
        return self._id

    @property
    def rep_id(self) -> str:
        """Returns associated Replication ID."""
        return self._rep_id

    def start(self):
        """Starts Replication worker."""
        return asyncio.async(self.changes_fetch_loop(
            self.changes_queue, self.reports_queue, self.target,
            batch_size=self.batch_size))

    @asyncio.coroutine
    def changes_fetch_loop(self,
                           changes_queue: WorkQueue,
                           reports_queue: WorkQueue,
                           target: ITargetPeer, *,
                           batch_size: int):
        # couch_replicator_worker:queue_fetch_loop/5
        while True:
            changes = yield from changes_queue.get(batch_size)

            if changes is changes_queue.CLOSED:
                break

            # Ensure that we report about the highest seq in the batch
            changes = sorted(changes, key=lambda i: i['seq'])
            report_seq = changes[-1]['seq']

            log.debug('Received batch of %d sequence(s) from %s to %s',
                      len(changes), changes[0], changes[-1],
                      extra={'rep_id': self.rep_id, 'worker_id': self.id})

            # Notify checkpoints_loop that we start work on the batch
            yield from reports_queue.put((False, report_seq))

            docid_missing = yield from self.find_missing_revs(target, changes)

            log.debug('Found %d missing revs for %d docs',
                      sum(map(len, docid_missing.values())), len(docid_missing),
                      extra={'rep_id': self.rep_id, 'worker_id': self.id})

            yield from reports_queue.put((True, report_seq))

    @asyncio.coroutine
    def find_missing_revs(self, target: ITargetPeer, changes: list) -> dict:
        # couch_replicator_worker:find_missing/2
        # Unlike couch_replicator we also remove duplicate revs from diff
        # request which may eventually when the same document with the conflicts
        # had updated multiple times within the same batch slice.
        docid_revs = defaultdict(list)
        seen = set()
        for docinfo in changes:
            docid = docinfo['id']
            for change in docinfo['changes']:
                rev = change['rev']
                if (docid, rev) in seen:
                    continue
                seen.add((docid, rev))
                docid_revs[docid].append(rev)

        revs_diff = yield from target.revs_diff(docid_revs)

        docid_missing = {}
        for docid, content in revs_diff.items():
            docid_missing[docid] = (content['missing'],
                                    content.get('possible_ancestors', []))
        return docid_missing