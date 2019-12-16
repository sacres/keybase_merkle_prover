import asyncio
from base64 import b64decode, b64encode
from dataclasses import dataclass, field, replace
from dataclasses_json import dataclass_json
from enum import Enum
import json
import logging
from typing import List

import pykeybasebot.types.chat1 as chat1

import kb_ots
import last_success
from merkle_root import fetch_keybase_merkle_root, MerkleRoot


class StampStatus(Enum):
    PRELIMINARY = "PRELIMINARY"
    VERIFIABLE = "VERIFIABLE"


@dataclass_json
@dataclass
class StampedMerkleRoot:
    # this is the object that gets serialized into a broadcasted keybase chat msg
    root: MerkleRoot
    ots: str = ""  # base64 encoded string of the bytes in the `.ots` file
    version: int = 0
    status: StampStatus = StampStatus.PRELIMINARY


async def broadcast_new_root(bot):
    logging.debug("broadcasting a new root")
    merkle_root = fetch_keybase_merkle_root()
    try:
        ots = await kb_ots.stamp(merkle_root.data_to_stamp)
    except kb_ots.StampError as e:
        logging.error(e)
        return

    stamped_root = StampedMerkleRoot(
        version=0,
        root=merkle_root,
        ots=ots,
        status=StampStatus.PRELIMINARY,
    )

    my_public_channel = chat1.ChatChannel(name=bot.username, public=True)
    res = await retry_if_timeout(bot.chat.send, my_public_channel, stamped_root.to_json())
    logging.info(f"broadcasted new root at msg_id {res.message_id}")


async def retry_if_timeout(func, *args, **kwargs):
    for i in range(0,100):
        try:
            result = await func(*args, **kwargs)
        except asyncio.TimeoutError:
            logging.error(f"got a timeout error on attempt {i+1}. retrying...")
            await asyncio.sleep(0.5)
            continue
        break
    else:
        raise asyncio.TimeoutError("retries exhausted :(")
    return result


async def update_messages(bot):
    channel = chat1.ChatChannel(name=bot.username, public=True)
    # TODO: paginate this more intelligently. I think Keybase will automatically
    # give the most recent 100 messages, which is probably fine to be honest.
    all_posts = await retry_if_timeout(bot.chat.read, channel)
    for m in reversed(all_posts):
        try:
            stamped_root = StampedMerkleRoot.from_json(m.content.text.body)
            msg_id = m.id
        except Exception:
            logging.debug(f"couldn't parse message {m.id}. nothing to do here...")
            continue
        if stamped_root.version != 0:
            logging.debug(f"message {m.id} has an old version. nothing to do here...")
            continue
        logging.debug(f"message {m.id} has status {stamped_root.status}")
        if stamped_root.status is StampStatus.PRELIMINARY:
            await update_ots_for_msg(bot, msg_id, stamped_root)
            await asyncio.sleep(1)  # necessary for broadcasting to succeed :/


async def update_ots_for_msg(bot, msg_id, stamped_root):
    logging.debug(f"msg {msg_id} is not yet on chain")
    ots_data = b64decode(stamped_root.ots)

    try:
        completed_ots = await kb_ots.upgrade(
            identifier=msg_id,
            raw_data=stamped_root.root.data_to_stamp,
            ots_data=ots_data,
        )
    except (kb_ots.VerifyError, kb_ots.UpgradeError) as e:
        logging.info(f"{msg_id} is not yet ready: {e}")
        logging.debug(e)
        return

    verifiable_stamp = replace(stamped_root,
        status=StampStatus.VERIFIABLE,
        ots=completed_ots,
    )
    channel = chat1.ChatChannel(name=bot.username, public=True)

    # edit the message with the new deets
    seqno = verifiable_stamp.root.seqno
    try:
        res = await retry_if_timeout(bot.chat.edit, channel, msg_id, verifiable_stamp.to_json())
    except Exception as e:
        logging.error(f"got an error broadcasting verifiable stamp at seqno: {seqno}, msg_id: {msg_id}")
        raise
    logging.info(f"{msg_id} is now verifiable")
    await last_success.update(bot, seqno)
