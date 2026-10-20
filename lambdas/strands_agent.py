import os
import logging

from strands import Agent

from tools.case import create_case, get_case, list_cases, record_validation
from tools.mail import send_contractor_email
from tools.ncr import search_ncr_history
from tools.notify import request_expert_validation
from utils.locales import MESSAGES


STARTUP_LOCALE = os.environ['LOCALE']
DEFAULT_MODEL = os.environ['DEFAULT_MODEL']
# Logger configuration
logger = logging.getLogger()

AGENT_TOOLS = [
    search_ncr_history,
    create_case,
    get_case,
    list_cases,
    request_expert_validation,
    record_validation,
    send_contractor_email,
]


class StrandsAgent():
    def __init__(self):
        pass

    def get_agent_with_history(self, messages, system_prompt):
        self.agent = Agent(
            messages=messages,
            system_prompt=system_prompt,
            tools=AGENT_TOOLS,
            model=DEFAULT_MODEL,
        )

    def agent_invoke(self, user_prompt, history=None, session_note=""):
        try:
            if history is not None:
                # Replays the stored conversation, which already carries the
                # session line captured when this sender's chat started.
                self.get_agent_with_history(
                    messages=history["messages"],
                    system_prompt=history["system_prompt"],
                )
            else:
                self.agent = Agent(
                    system_prompt=MESSAGES[STARTUP_LOCALE]["system"] + session_note,
                    tools=AGENT_TOOLS,
                    model=DEFAULT_MODEL,
                )
            result = self.agent(user_prompt)
            logger.info(f"Agent result: {result}")
            return result, self.agent.messages, self.agent.system_prompt
        except Exception as e:
            logger.info(f"Error during agent invocation: {e}")
            raise
