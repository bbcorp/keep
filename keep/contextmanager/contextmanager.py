# TODO - refactor context manager to support multitenancy in a more robust way
import logging
from typing import Any, TypedDict

import click
import json5
from pympler.asizeof import asizeof

from keep.api.core.config import config
from keep.api.core.db import get_last_workflow_execution_by_workflow_id, get_session
from keep.api.models.alert import AlertDto
from keep.api.models.incident import IncidentDto


class ForeachContext(TypedDict):
    value: Any | None
    items: list[Any] | None


class ContextManager:
    def __init__(
        self,
        tenant_id,
        workflow_id=None,
        workflow_execution_id=None,
        workflow: dict | None = None,
    ):
        self.logger = logging.getLogger(__name__)
        self.workflow_id = workflow_id
        self.workflow_execution_id = workflow_execution_id
        self.tenant_id = tenant_id
        self.steps_context = {}
        self.steps_context_size = 0
        self.providers_context = {}
        self.actions_context = {}
        self.event_context: AlertDto = {}
        self.incident_context: IncidentDto | None = None
        self.foreach_context: ForeachContext = {
            "value": None,
            "items": None,
        }
        self.consts_context = {}
        self.current_step_vars = {}
        self.current_step_aliases = {}
        self.secret_context = {}
        # cli context
        try:
            self.click_context = click.get_current_context()
        except RuntimeError:
            self.click_context = {}
        # last workflow context
        self.last_workflow_execution_results = {}
        self.last_workflow_run_time = None
        if self.workflow_id and workflow:
            try:
                # @tb: try to understand if the workflow tries to use last_workflow_results
                # if so, we need to get the last workflow execution and load it into the context
                workflow_str = json5.dumps(workflow)
                last_workflow_results_in_workflow = (
                    "last_workflow_results" in workflow_str
                    or "last_workflow_run_time" in workflow_str
                )
                if last_workflow_results_in_workflow:
                    last_workflow_execution = (
                        get_last_workflow_execution_by_workflow_id(
                            tenant_id, workflow_id, status="success"
                        )
                    )
                    if last_workflow_execution is not None:
                        self.last_workflow_execution_results = (
                            last_workflow_execution.results
                        )
                        self.last_workflow_run_time = last_workflow_execution.started
            except Exception:
                self.logger.exception("Failed to get last workflow execution")
                pass
        self.aliases = {}
        # dependencies are used so iohandler will be able to use the output class of the providers
        # e.g. let's say bigquery_provider results are google.cloud.bigquery.Row
        #     and we want to use it in iohandler, we need to import it before the eval
        self.dependencies = set()
        self.workflow_execution_id = None
        self.workflow_inputs = None
        self._api_key = None
        self.__loggers = {}

    @property
    def api_url(self):
        """
        The URL of the Keep API
        """
        return config("KEEP_API_URL")

    @property
    def api_key(self):
        # avoid circular import
        from keep.api.utils.tenant_utils import get_or_create_api_key

        if self._api_key is None:
            session = next(get_session())
            self._api_key = get_or_create_api_key(
                session=session,
                created_by="system",
                tenant_id=self.tenant_id,
                unique_api_key_id="webhook",
            )
            session.close()
        return self._api_key

    def set_execution_context(self, workflow_id, workflow_execution_id):
        self.workflow_execution_id = workflow_execution_id
        self.workflow_id = workflow_id
        for logger in self.__loggers.values():
            logger.workflow_execution_id = workflow_execution_id

    def set_inputs(self, inputs):
        self.workflow_inputs = inputs

    def set_event_context(self, event):
        self.event_context = event

    def set_incident_context(self, incident):
        self.incident_context = incident

    def set_consts_context(self, consts):
        self.consts_context = consts

    def get_workflow_id(self):
        return self.workflow_id

    def set_secret_context(self):
        """
        Set the secret context for the workflow.
        If no secret is provided, attempt to load it from the secret manager.
        """
        from keep.secretmanager.secretmanagerfactory import SecretManagerFactory

        secret_manager = SecretManagerFactory.get_secret_manager(self)

        secret_key = f"{self.tenant_id}_{self.workflow_id}_secrets"
        try:
            secret = secret_manager.read_secret(secret_name=secret_key, is_json=True)
            self.secret_context = secret or {}
        except Exception:
            self.logger.warning(
                "Could not load secrets for workflow",
                extra={"workflow_id": self.workflow_id, "tenant_id": self.tenant_id},
            )
            self.secret_context = {}

    def get_full_context(self, exclude_providers=False, exclude_env=False):
        """
        Gets full context on the workflows

        Usage: context injection used, for example, in iohandler

        Returns:
            dict: dictinoary contains all context about this workflow
                  providers - all context about providers (configuration, etc)
                  steps - all context about steps (output, conditions, etc)
                  foreach - all context about the current 'foreach'
                            foreach can be in two modes:
                                1. "step foreach" - for step result
                                2. "condition foreach" - for each condition result
                            whereas in (2), the {{ foreach.value }} contains (1), in the (1) case, we need to explicitly put in under (value)
                            anyway, this should be refactored to something more structured
        """
        full_context = {
            "steps": self.steps_context,
            "actions": self.steps_context,  # this is an alias for steps
            "foreach": self.foreach_context,
            "event": self.event_context,
            "last_workflow_results": self.last_workflow_execution_results,
            "last_workflow_run_time": self.last_workflow_run_time,
            "alert": self.event_context,  # this is an alias so workflows will be able to use alert.source
            "incident": self.incident_context,  # this is an alias so workflows will be able to use alert.source
            "consts": self.consts_context,
            "vars": self.current_step_vars,
            "aliases": self.current_step_aliases,
            "secrets": self.secret_context,
            "inputs": self.workflow_inputs,
        }

        if not exclude_providers:
            full_context["providers"] = self.providers_context

        full_context.update(self.aliases)
        return full_context

    def set_foreach_items(self, items: list[Any] | None = None):
        self.foreach_context["items"] = items

    def set_foreach_value(self, value: Any | None = None):
        self.foreach_context["value"] = value

    def reset_foreach_context(self):
        self.foreach_context = {
            "value": None,
            "items": None,
        }

    def set_condition_results(
        self,
        action_id,
        condition_name,
        condition_type,
        compare_to,
        compare_value,
        result,
        condition_alias=None,
        value=None,
        **kwargs,
    ):
        """_summary_

        Args:
            action_id (_type_): id of the step
            condition_type (_type_): type of the condition
            compare_to (_type_): _description_
            compare_value (_type_): _description_
            result (_type_): _description_
            condition_alias (_type_, optional): _description_. Defaults to None.
            value (_type_): the raw value which the condition was compared to. this is relevant only for foreach conditions
        """
        if action_id not in self.steps_context:
            self.steps_context[action_id] = {"conditions": {}, "results": {}}
        if "conditions" not in self.steps_context[action_id]:
            self.steps_context[action_id]["conditions"] = {condition_name: []}
        if condition_name not in self.steps_context[action_id]["conditions"]:
            self.steps_context[action_id]["conditions"][condition_name] = []

        self.steps_context[action_id]["conditions"][condition_name].append(
            {
                "value": value,
                "compare_value": compare_value,
                "compare_to": compare_to,
                "result": result,
                "type": condition_type,
                "alias": condition_alias,
                **kwargs,
            }
        )
        # update the current for each context
        self.foreach_context.update(
            {"compare_value": compare_value, "compare_to": compare_to, **kwargs}
        )
        if condition_alias:
            self.aliases[condition_alias] = result

    def set_step_provider_paremeters(self, step_id, provider_parameters):
        if step_id not in self.steps_context:
            self.steps_context[step_id] = {
                "provider_parameters": {},
                "results": [],
                "vars": {},
            }
        self.steps_context[step_id]["provider_parameters"] = provider_parameters

    def set_step_context(self, step_id, results, foreach=False):
        if step_id not in self.steps_context:
            self.steps_context[step_id] = {
                "provider_parameters": {},
                "results": [],
                "vars": {},
            }

        # If this is a foreach step, we need to append the results to the list
        # so we can iterate over them
        if foreach:
            self.steps_context[step_id]["results"].append(results)
        else:
            self.steps_context[step_id]["results"] = results
        # this is an alias to the current step output
        self.steps_context["this"] = self.steps_context[step_id]
        self.steps_context_size = asizeof(self.steps_context)

    def set_step_vars(self, step_id, _vars, _aliases):
        if step_id not in self.steps_context:
            self.steps_context[step_id] = {
                "provider_parameters": {},
                "results": [],
                "vars": {},
                "aliases": {},
            }

        self.current_step_vars = _vars
        self.current_step_aliases = _aliases
        self.steps_context[step_id]["vars"] = _vars
        self.steps_context[step_id]["aliases"] = _aliases
        self.secret_context = {**self.secret_context, **_vars}

    def get_last_workflow_run(self, workflow_id):
        return get_last_workflow_execution_by_workflow_id(self.tenant_id, workflow_id)

    def set_last_workflow_run(self, workflow_id, workflow_context, workflow_status):
        # TODO: move to DB
        # self.logger.debug(
        #     "Adding workflow to state",
        #     extra={
        #         "workflow_id": workflow_id,
        #     },
        # )
        # if workflow_id not in self.state:
        #     self.state[workflow_id] = []
        # self.state[workflow_id].append(
        #     {
        #         "workflow_status": workflow_status,
        #         "workflow_context": workflow_context,
        #     }
        # )
        # self.logger.debug(
        #     "Added workflow to state",
        #     extra={
        #         "workflow_id": workflow_id,
        #     },
        # )
        pass
