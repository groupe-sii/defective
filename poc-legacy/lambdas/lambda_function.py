import decimal
import json
import logging
import os
import re
import time
from datetime import datetime

from strands_agent import StrandsAgent
from utils import dispatcher
from utils.dynamo import DynamoDB
from utils.locales import MESSAGES
from utils.messages import parse_sns_message
from tools.context import clear_sender, session_note, set_sender


# Logger configuration
logger = logging.getLogger()
logger.setLevel(logging.INFO)


# DynamoDB table name
user_history_table = os.environ["USER_HISTORY_TABLE"]
locale = os.environ["LOCALE"]

# Conformity experts (validation chat role): numbers allowed to approve/reject
# procedures. Every other sender is treated as a technician.
expert_contacts = {
    number.strip().lstrip("+")
    for number in os.environ.get("EXPERT_CONTACTS", "").split(",")
    if number.strip()
}


def resolve_role(phone_number):
    return "expert" if str(phone_number).lstrip("+") in expert_contacts else "technician"


def sanitize_decimals(value):
    """DynamoDB returns every number as decimal.Decimal. Strands re-validates
    stored toolUse inputs when replaying history and rejects Decimal (not a
    JSON type), so convert back to int/float before handing history over."""
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: sanitize_decimals(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_decimals(v) for v in value]
    return value


def build_history_row(message, messages, role, system_prompt):
    """One row per identity + day. `phone_number` stays the DynamoDB key name
    for backwards compatibility; it now holds the normalized identity."""
    now = datetime.now()
    return {
        "phone_number": message.identity,
        "day": now.strftime("%Y/%m/%d"),
        "session_time": int(time.time()),
        "channel": message.channel,
        "message_id": message.message_id,
        "recipient": message.recipient,
        "channel_meta": message.channel_meta,
        "messages": messages,
        "system_prompt": system_prompt,
    }


def process_record(record, sns_client=None):
    sns = record.get("Sns", {})
    raw = sns.get("Message", "{}")
    if not raw:
        logger.error("Empty SNS record: %s", record)
        return None
    sns_message = json.loads(raw, parse_float=decimal.Decimal)

    inbound_messages = parse_sns_message(sns_message)
    if not inbound_messages:
        logger.error("No inbound message could be parsed from SNS record")
        return None

    dynamo = DynamoDB()
    bedrock = StrandsAgent()

    for message in inbound_messages:
        # Resolve who is talking BEFORE invoking the agent: tools gate on this
        # context (record_validation is expert-only) and the session line in
        # the system prompt tells the model who it is chatting with.
        role = resolve_role(message.sender)
        set_sender(role, message.sender, message.channel)
        logger.info("Sender %s on %s resolved as %s",
                    message.sender, message.channel, role)
        try:
            if not message.is_text:
                logger.error("Unsupported message type: %s", message.message_type)
                _publish_failure(sns_client, f"Unsupported message type: {message.message_type}")
                return 500

            # load history (keyed by identity, so channels share a conversation)
            history = dynamo.query_by_day(user_history_table, message.identity)
            hist_build = history[0] if history else None
            if hist_build is not None:
                hist_build["messages"] = sanitize_decimals(hist_build["messages"])

            llm_response, agent_messages, sys_prompt = bedrock.agent_invoke(
                message.text, hist_build, session_note()
            )

            row = build_history_row(message, agent_messages, "assistant", sys_prompt)
            dynamo.insert_user_message(user_history_table, row)

            reply_text = remove_thinking_tags(llm_response.message["content"][0]["text"])
            data = {
                "channel": message.channel,
                "phone_number": message.identity,
                "message_id": message.message_id,
                "message": reply_text,
                "metadata": message.channel_meta,
                "reply": dispatcher.send_reply(
                    channel=message.channel,
                    sender=message.identity,
                    text=reply_text,
                    phone_number_id=message.channel_meta.get("phone_number_id", ""),
                    message_id=message.message_id,
                    origination_number=message.recipient,
                ),
            }
            logger.info("Message processed successfully")
            return data
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            _publish_failure(sns_client, f"Error processing message: {e}")
            raise e
        finally:
            # Lambda execution environments are reused across invocations:
            # never leak the sender context into the next message.
            clear_sender()


def _publish_failure(sns_client, detail):
    if sns_client is None or not os.environ.get("sns_topic_arn"):
        return
    try:
        sns_client.publish(
            TopicArn=os.environ["sns_topic_arn"],
            Subject="Text-to-speech function failure",
            Message=detail,
        )
    except Exception as err:
        logger.error("Failed to publish failure notification: %s", err)


def remove_thinking_tags(text):
    try:
        # Remove everything between tags <thinking></thinking>
        cleaned_text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
        # Remove whitespaces
        cleaned_text = cleaned_text.strip()
        return cleaned_text
    except Exception as e:
        logger.error(f"Error removing thinking tags: {str(e)}")
        raise


def lambda_handler(event, context):
    try:
        records = event.get("Records", [])
        results = []
        import boto3

        sns_client = boto3.client("sns")
        for record in records:
            results.append(process_record(record, sns_client))
        return {"statusCode": 200, "body": json.dumps("Success", default=str)}
    except Exception as e:
        logger.error(f"Error processing event: {str(e)}")
        return {"statusCode": 500, "body": json.dumps("Error processing request")}
