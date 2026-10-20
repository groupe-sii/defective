import decimal
import json
import logging
import os
import re

from strands_agent import StrandsAgent
from utils.dynamo import DynamoDB
from utils.whatsapp import WhatsappService
from utils.locales import MESSAGES
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


def process_record(record):
    sns = record.get("Sns", {})
    sns_message = json.loads(sns.get("Message", "{}"), parse_float=decimal.Decimal)
    print(f"sns_message: {sns_message}")

    whatsapp_info = WhatsappService(sns_message)
    dynamo = DynamoDB()
    bedrock = StrandsAgent()

    for message in whatsapp_info.messages:
        # Resolve who is talking BEFORE invoking the agent: tools gate on this
        # context (record_validation is expert-only) and the session line in
        # the system prompt tells the model who it is chatting with.
        sender = message.phone_number
        role = resolve_role(sender)
        set_sender(role, sender, "whatsapp")
        logger.info("Sender %s resolved as %s", sender, role)
        try:
            message_type = message.message.get("type")
            print("type:", message_type)

            if message_type == "text":
                data = {
                    "message": message.message,
                    "id": message.message_id,
                    "phone_number": message.phone_number,
                    "phone_number_id": message.phone_number_id,
                    "metadata": message.metadata,
                }
            else:
                data = {
                    "message": MESSAGES[locale]["error"],
                    "phone_number_id": message.phone_number_id,
                    "metadata": message.metadata,
                }
                logger.error("Error on input. Not text")
                return {
                    "statusCode": 500,
                    "body": json.dumps(
                        "Error processing input type. Video, audio, image not supported yet."
                    ),
                }

            # get history
            history = dynamo.query_by_day(user_history_table, message.phone_number)
            # print(f'History: {history}')
            hist_build = (
                history[0] if history != [] else None
            )
            if hist_build is not None:
                hist_build["messages"] = sanitize_decimals(hist_build["messages"])
            print(f"History after Build: {hist_build}")

            # invoking agent
            llm_response, agent_messages, sys_prompt = bedrock.agent_invoke(
                message.get_text(), hist_build, session_note()
            )
            # logger.info(f'LLM answer: {llm_response}')

            # Creating new row with bedrock response (for logging)
            row = message.build_whatsapp_row(
                phone_number=message.phone_number,
                messages=agent_messages,
                role="assistant",
                meta_phone_number_id=message.meta_phone_number_id,
                id=message.message_id,
                phone_number_id=message.phone_number_id,
                timestamp=message.timestamp,
                system_prompt=sys_prompt,
            )

            # Insert Log on Dynamo (LLM answer)
            ret = dynamo.insert_user_message(user_history_table, row)

            data["message"] = remove_thinking_tags(
                llm_response.message["content"][0]["text"]
            )
            logger.info("Message processed successfully")

            whatsapp_info.text_reply(
                data["phone_number"], data["id"], data["phone_number_id"], data["message"]
            )
        finally:
            # Lambda execution environments are reused across invocations:
            # never leak the sender context into the next message.
            clear_sender()


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
        for record in records:
            process_record(record)
        return {"statusCode": 200, "body": json.dumps("Success")}
    except Exception as e:
        logger.error(f"Error processing event: {str(e)}")
        return {"statusCode": 500, "body": json.dumps("Error processing request")}
