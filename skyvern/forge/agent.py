import asyncio
import json
import random
from datetime import datetime
from typing import Any, Tuple

import requests
import structlog
from playwright._impl._errors import TargetClosedError

from skyvern import analytics
from skyvern.exceptions import (
    BrowserStateMissingPage,
    FailedToSendWebhook,
    InvalidWorkflowTaskURLState,
    MissingBrowserStatePage,
    TaskNotFound,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.agent import Agent
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.security import generate_skyvern_signature
from skyvern.forge.sdk.models import Organization, Step, StepStatus
from skyvern.forge.sdk.schemas.tasks import Task, TaskRequest, TaskStatus
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import TaskBlock
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRun
from skyvern.webeye.actions.actions import (
    Action,
    ActionType,
    CompleteAction,
    UserDefinedError,
    WebAction,
    parse_actions,
)
from skyvern.webeye.actions.handler import ActionHandler
from skyvern.webeye.actions.models import AgentStepOutput, DetailedAgentStepOutput
from skyvern.webeye.actions.responses import ActionResult
from skyvern.webeye.browser_factory import BrowserState
from skyvern.webeye.scraper.scraper import ScrapedPage, scrape_website

LOG = structlog.get_logger()


class ForgeAgent(Agent):
    def __init__(self) -> None:
        if SettingsManager.get_settings().ADDITIONAL_MODULES:
            for module in SettingsManager.get_settings().ADDITIONAL_MODULES:
                LOG.info("Loading additional module", module=module)
                __import__(module)
            LOG.info("Additional modules loaded", modules=SettingsManager.get_settings().ADDITIONAL_MODULES)
        LOG.info(
            "Initializing ForgeAgent",
            env=SettingsManager.get_settings().ENV,
            execute_all_steps=SettingsManager.get_settings().EXECUTE_ALL_STEPS,
            browser_type=SettingsManager.get_settings().BROWSER_TYPE,
            max_scraping_retries=SettingsManager.get_settings().MAX_SCRAPING_RETRIES,
            video_path=SettingsManager.get_settings().VIDEO_PATH,
            browser_action_timeout_ms=SettingsManager.get_settings().BROWSER_ACTION_TIMEOUT_MS,
            max_steps_per_run=SettingsManager.get_settings().MAX_STEPS_PER_RUN,
            long_running_task_warning_ratio=SettingsManager.get_settings().LONG_RUNNING_TASK_WARNING_RATIO,
            debug_mode=SettingsManager.get_settings().DEBUG_MODE,
        )

    async def validate_step_execution(
        self,
        task: Task,
        step: Step,
    ) -> None:
        """
        Checks if the step can be executed.
        :return: A tuple of whether the step can be executed and a list of reasons why it can't be executed.
        """
        reasons = []
        # can't execute if task status is not running
        has_valid_task_status = task.status == TaskStatus.running
        if not has_valid_task_status:
            reasons.append(f"invalid_task_status:{task.status}")
        # can't execute if the step is already running or completed
        has_valid_step_status = step.status in [StepStatus.created, StepStatus.failed]
        if not has_valid_step_status:
            reasons.append(f"invalid_step_status:{step.status}")
        # can't execute if the task has another step that is running
        steps = await app.DATABASE.get_task_steps(task_id=task.task_id, organization_id=task.organization_id)
        has_no_running_steps = not any(step.status == StepStatus.running for step in steps)
        if not has_no_running_steps:
            reasons.append(f"another_step_is_running_for_task:{task.task_id}")

        can_execute = has_valid_task_status and has_valid_step_status and has_no_running_steps
        if not can_execute:
            raise Exception(f"Cannot execute step. Reasons: {reasons}, Step: {step}")

    async def create_task_and_step_from_block(
        self,
        task_block: TaskBlock,
        workflow: Workflow,
        workflow_run: WorkflowRun,
        workflow_run_context: WorkflowRunContext,
        task_order: int,
        task_retry: int,
    ) -> tuple[Task, Step]:
        task_block_parameters = task_block.parameters
        navigation_payload = {}
        for parameter in task_block_parameters:
            navigation_payload[parameter.key] = workflow_run_context.get_value(parameter.key)

        task_url = task_block.url
        if task_url is None:
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(workflow_run=workflow_run)
            if not browser_state.page:
                LOG.error("BrowserState has no page", workflow_run_id=workflow_run.workflow_run_id)
                raise MissingBrowserStatePage(workflow_run_id=workflow_run.workflow_run_id)

            if browser_state.page.url == "about:blank":
                raise InvalidWorkflowTaskURLState(workflow_run.workflow_run_id)

            task_url = browser_state.page.url

        task = await app.DATABASE.create_task(
            url=task_url,
            title=task_block.title,
            webhook_callback_url=None,
            navigation_goal=task_block.navigation_goal,
            data_extraction_goal=task_block.data_extraction_goal,
            navigation_payload=navigation_payload,
            organization_id=workflow.organization_id,
            proxy_location=workflow_run.proxy_location,
            extracted_information_schema=task_block.data_schema,
            workflow_run_id=workflow_run.workflow_run_id,
            order=task_order,
            retry=task_retry,
            error_code_mapping=task_block.error_code_mapping,
        )
        LOG.info(
            "Created new task for workflow run",
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            task_id=task.task_id,
            url=task.url,
            title=task.title,
            nav_goal=task.navigation_goal,
            data_goal=task.data_extraction_goal,
            error_code_mapping=task.error_code_mapping,
            proxy_location=task.proxy_location,
            task_order=task_order,
            task_retry=task_retry,
        )
        # Update task status to running
        task = await app.DATABASE.update_task(
            task_id=task.task_id, organization_id=task.organization_id, status=TaskStatus.running
        )
        step = await app.DATABASE.create_step(
            task.task_id,
            order=0,
            retry_index=0,
            organization_id=task.organization_id,
        )
        LOG.info(
            "Created new step for workflow run",
            workflow_id=workflow.workflow_id,
            workflow_run_id=workflow_run.workflow_run_id,
            step_id=step.step_id,
            task_id=task.task_id,
            order=step.order,
            retry_index=step.retry_index,
        )
        return task, step

    async def create_task(self, task_request: TaskRequest, organization_id: str | None = None) -> Task:
        task = await app.DATABASE.create_task(
            url=task_request.url,
            title=task_request.title,
            webhook_callback_url=task_request.webhook_callback_url,
            navigation_goal=task_request.navigation_goal,
            data_extraction_goal=task_request.data_extraction_goal,
            navigation_payload=task_request.navigation_payload,
            organization_id=organization_id,
            proxy_location=task_request.proxy_location,
            extracted_information_schema=task_request.extracted_information_schema,
            error_code_mapping=task_request.error_code_mapping,
        )
        LOG.info(
            "Created new task",
            task_id=task.task_id,
            title=task.title,
            url=task.url,
            nav_goal=task.navigation_goal,
            data_goal=task.data_extraction_goal,
            proxy_location=task.proxy_location,
        )
        return task

    async def execute_step(
        self,
        organization: Organization,
        task: Task,
        step: Step,
        api_key: str | None = None,
        workflow_run: WorkflowRun | None = None,
        close_browser_on_completion: bool = True,
    ) -> Tuple[Step, DetailedAgentStepOutput | None, Step | None]:
        next_step: Step | None = None
        detailed_output: DetailedAgentStepOutput | None = None
        try:
            # Check some conditions before executing the step, throw an exception if the step can't be executed
            await self.validate_step_execution(task, step)
            step, browser_state, detailed_output = await self._initialize_execution_state(task, step, workflow_run)
            step, detailed_output = await self.agent_step(task, step, browser_state, organization=organization)
            task = await self.update_task_errors_from_detailed_output(task, detailed_output)
            retry = False

            # If the step failed, mark the step as failed and retry
            if step.status == StepStatus.failed:
                maybe_next_step = await self.handle_failed_step(task, step)
                # If there is no next step, it means that the task has failed
                if maybe_next_step:
                    next_step = maybe_next_step
                    retry = True
                else:
                    await self.send_task_response(
                        task=task,
                        last_step=step,
                        api_key=api_key,
                        close_browser_on_completion=close_browser_on_completion,
                    )
                    return step, detailed_output, None
            elif step.status == StepStatus.completed:
                # TODO (kerem): keep the task object uptodate at all times so that send_task_response can just use it
                is_task_completed, maybe_last_step, maybe_next_step = await self.handle_completed_step(
                    organization, task, step
                )
                if is_task_completed is not None and maybe_last_step:
                    last_step = maybe_last_step
                    await self.send_task_response(
                        task=task,
                        last_step=last_step,
                        api_key=api_key,
                        close_browser_on_completion=close_browser_on_completion,
                    )
                    return last_step, detailed_output, None
                elif maybe_next_step:
                    next_step = maybe_next_step
                    retry = False
                else:
                    LOG.error(
                        "Step completed but task is not completed and next step is not created.",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        is_task_completed=is_task_completed,
                        maybe_last_step=maybe_last_step,
                        maybe_next_step=maybe_next_step,
                    )
            else:
                LOG.error(
                    "Unexpected step status after agent_step",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    step_status=step.status,
                )

            if retry and next_step:
                return await self.execute_step(
                    organization,
                    task,
                    next_step,
                    api_key=api_key,
                )
            elif SettingsManager.get_settings().execute_all_steps() and next_step:
                return await self.execute_step(
                    organization,
                    task,
                    next_step,
                    api_key=api_key,
                )
            else:
                LOG.info(
                    "Step executed but continuous execution is disabled.",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    is_cloud_env=SettingsManager.get_settings().is_cloud_environment(),
                    execute_all_steps=SettingsManager.get_settings().execute_all_steps(),
                    next_step_id=next_step.step_id if next_step else None,
                )

            return step, detailed_output, next_step
        # TODO (kerem): Let's add other exceptions that we know about here as custom exceptions as well
        except FailedToSendWebhook:
            LOG.exception(
                "Failed to send webhook",
                exc_info=True,
                task_id=task.task_id,
                step_id=step.step_id,
                task=task,
                step=step,
            )
            return step, detailed_output, next_step

    async def agent_step(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState,
        organization: Organization | None = None,
    ) -> tuple[Step, DetailedAgentStepOutput]:
        detailed_agent_step_output = DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=None,
            action_results=None,
            actions_and_results=None,
        )
        try:
            LOG.info(
                "Starting agent step",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
            )
            step = await self.update_step(step=step, status=StepStatus.running)
            scraped_page, extract_action_prompt = await self._build_and_record_step_prompt(
                task,
                step,
                browser_state,
            )
            detailed_agent_step_output.scraped_page = scraped_page
            detailed_agent_step_output.extract_action_prompt = extract_action_prompt
            json_response = None
            actions: list[Action]
            if task.navigation_goal:
                json_response = await app.LLM_API_HANDLER(
                    prompt=extract_action_prompt,
                    step=step,
                    screenshots=scraped_page.screenshots,
                )
                detailed_agent_step_output.llm_response = json_response

                actions = parse_actions(task, json_response["actions"])
            else:
                actions = [
                    CompleteAction(
                        reasoning="Task has no navigation goal.", data_extraction_goal=task.data_extraction_goal
                    )
                ]
            detailed_agent_step_output.actions = actions
            if len(actions) == 0:
                LOG.info(
                    "No actions to execute, marking step as failed",
                    task_id=task.task_id,
                    step_id=step.step_id,
                    step_order=step.order,
                    step_retry=step.retry_index,
                )
                step = await self.update_step(
                    step=step, status=StepStatus.failed, output=detailed_agent_step_output.to_agent_step_output()
                )
                detailed_agent_step_output = DetailedAgentStepOutput(
                    scraped_page=scraped_page,
                    extract_action_prompt=extract_action_prompt,
                    llm_response=json_response,
                    actions=actions,
                    action_results=[],
                    actions_and_results=[],
                )
                return step, detailed_agent_step_output

            # Execute the actions
            LOG.info(
                "Executing actions",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                actions=actions,
            )
            action_results: list[ActionResult] = []
            detailed_agent_step_output.action_results = action_results
            # filter out wait action if there are other actions in the list
            # we do this because WAIT action is considered as a failure
            # which will block following actions if we don't remove it from the list
            # if the list only contains WAIT action, we will execute WAIT action(s)
            if len(actions) > 1:
                wait_actions_to_skip = [action for action in actions if action.action_type == ActionType.WAIT]
                wait_actions_len = len(wait_actions_to_skip)
                # if there are wait actions and there are other actions in the list, skip wait actions
                if wait_actions_len > 0 and wait_actions_len < len(actions):
                    actions = [action for action in actions if action.action_type != ActionType.WAIT]
                    LOG.info("Skipping wait actions", wait_actions_to_skip=wait_actions_to_skip, actions=actions)

            # initialize list of tuples and set actions as the first element of each tuple so that in the case
            # of an exception, we can still see all the actions
            detailed_agent_step_output.actions_and_results = [(action, []) for action in actions]

            web_action_element_ids = set()
            for action_idx, action in enumerate(actions):
                if isinstance(action, WebAction):
                    if action.element_id in web_action_element_ids:
                        LOG.error("Duplicate action element id. Action handling stops", action=action)
                        break
                    web_action_element_ids.add(action.element_id)

                results = await ActionHandler.handle_action(scraped_page, task, step, browser_state, action)
                detailed_agent_step_output.actions_and_results[action_idx] = (action, results)
                # wait random time between actions to avoid detection
                await asyncio.sleep(random.uniform(1.0, 2.0))
                await self.record_artifacts_after_action(task, step, browser_state)
                for result in results:
                    result.step_retry_number = step.retry_index
                    result.step_order = step.order
                action_results.extend(results)
                # Check the last result for this action. If that succeeded, assume the entire action is successful
                if results and results[-1].success:
                    LOG.info(
                        "Action succeeded",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        step_retry=step.retry_index,
                        action_idx=action_idx,
                        action=action,
                        action_result=results,
                    )
                    # if the action triggered javascript calls
                    # this action should be the last action this round and do not take more actions.
                    # for now, we're being optimistic and assuming that
                    # js call doesn't have impact on the following actions
                    if results[-1].javascript_triggered:
                        LOG.info("Action triggered javascript. Stop executing reamaining actions.", action=action)
                        # stop executing the rest actions
                        break
                else:
                    LOG.warning(
                        "Action failed, marking step as failed",
                        task_id=task.task_id,
                        step_id=step.step_id,
                        step_order=step.order,
                        step_retry=step.retry_index,
                        action_idx=action_idx,
                        action=action,
                        action_result=results,
                        actions_and_results=detailed_agent_step_output.actions_and_results,
                    )
                    # if the action failed, don't execute the rest of the actions, mark the step as failed, and retry
                    failed_step = await self.update_step(
                        step=step, status=StepStatus.failed, output=detailed_agent_step_output.to_agent_step_output()
                    )
                    return failed_step, detailed_agent_step_output

            LOG.info(
                "Actions executed successfully, marking step as completed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                action_results=action_results,
            )
            # If no action errors return the agent state and output
            completed_step = await self.update_step(
                step=step, status=StepStatus.completed, output=detailed_agent_step_output.to_agent_step_output()
            )
            return completed_step, detailed_agent_step_output
        except Exception:
            LOG.exception(
                "Unexpected exception in agent_step, marking step as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
            )
            failed_step = await self.update_step(
                step=step, status=StepStatus.failed, output=detailed_agent_step_output.to_agent_step_output()
            )
            return failed_step, detailed_agent_step_output

    async def record_artifacts_after_action(self, task: Task, step: Step, browser_state: BrowserState) -> None:
        if not browser_state.page:
            raise BrowserStateMissingPage()
        try:
            screenshot = await browser_state.take_screenshot(full_page=True)
            await app.ARTIFACT_MANAGER.create_artifact(
                step=step,
                artifact_type=ArtifactType.SCREENSHOT_ACTION,
                data=screenshot,
            )
        except Exception:
            LOG.error(
                "Failed to record screenshot after action",
                task_id=task.task_id,
                step_id=step.step_id,
                exc_info=True,
            )

        try:
            html = await browser_state.page.content()
            await app.ARTIFACT_MANAGER.create_artifact(
                step=step,
                artifact_type=ArtifactType.HTML_ACTION,
                data=html.encode(),
            )
        except Exception:
            LOG.error(
                "Failed to record html after action",
                task_id=task.task_id,
                step_id=step.step_id,
                exc_info=True,
            )

        try:
            video_data = await app.BROWSER_MANAGER.get_video_data(task_id=task.task_id, browser_state=browser_state)
            await app.ARTIFACT_MANAGER.update_artifact_data(
                artifact_id=browser_state.browser_artifacts.video_artifact_id,
                organization_id=task.organization_id,
                data=video_data,
            )
        except Exception:
            LOG.error(
                "Failed to record video after action",
                task_id=task.task_id,
                step_id=step.step_id,
                exc_info=True,
            )

    async def _initialize_execution_state(
        self, task: Task, step: Step, workflow_run: WorkflowRun | None = None
    ) -> tuple[Step, BrowserState, DetailedAgentStepOutput]:
        if workflow_run:
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_workflow_run(
                workflow_run=workflow_run, url=task.url
            )
        else:
            browser_state = await app.BROWSER_MANAGER.get_or_create_for_task(task)
        # Initialize video artifact for the task here, afterwards it'll only get updated
        if browser_state and not browser_state.browser_artifacts.video_artifact_id:
            video_data = await app.BROWSER_MANAGER.get_video_data(task_id=task.task_id, browser_state=browser_state)
            video_artifact_id = await app.ARTIFACT_MANAGER.create_artifact(
                step=step,
                artifact_type=ArtifactType.RECORDING,
                data=video_data,
            )
            app.BROWSER_MANAGER.set_video_artifact_for_task(task, video_artifact_id)

        detailed_output = DetailedAgentStepOutput(
            scraped_page=None,
            extract_action_prompt=None,
            llm_response=None,
            actions=None,
            action_results=None,
            actions_and_results=None,
        )
        return step, browser_state, detailed_output

    async def _build_and_record_step_prompt(
        self,
        task: Task,
        step: Step,
        browser_state: BrowserState,
    ) -> tuple[ScrapedPage, str]:
        # Scrape the web page and get the screenshot and the elements
        scraped_page = await scrape_website(
            browser_state,
            task.url,
        )
        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.HTML_SCRAPE,
            data=scraped_page.html.encode(),
        )
        LOG.info(
            "Scraped website",
            task_id=task.task_id,
            step_id=step.step_id,
            step_order=step.order,
            step_retry=step.retry_index,
            num_elements=len(scraped_page.elements),
            url=task.url,
        )
        # Get action results from the last app.SETTINGS.PROMPT_ACTION_HISTORY_WINDOW steps
        steps = await app.DATABASE.get_task_steps(task_id=task.task_id, organization_id=task.organization_id)
        window_steps = steps[-1 * SettingsManager.get_settings().PROMPT_ACTION_HISTORY_WINDOW :]
        action_results: list[ActionResult] = []
        for window_step in window_steps:
            if window_step.output and window_step.output.action_results:
                action_results.extend(window_step.output.action_results)
        action_results_str = json.dumps([action_result.model_dump() for action_result in action_results])
        # Generate the extract action prompt
        navigation_goal = task.navigation_goal
        extract_action_prompt = prompt_engine.load_prompt(
            "extract-action",
            navigation_goal=navigation_goal,
            navigation_payload_str=json.dumps(task.navigation_payload),
            url=task.url,
            elements=scraped_page.element_tree_trimmed,  # scraped_page.element_tree,
            data_extraction_goal=task.data_extraction_goal,
            action_history=action_results_str,
            error_code_mapping_str=json.dumps(task.error_code_mapping) if task.error_code_mapping else None,
            utc_datetime=datetime.utcnow(),
        )

        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.VISIBLE_ELEMENTS_ID_XPATH_MAP,
            data=json.dumps(scraped_page.id_to_xpath_dict, indent=2).encode(),
        )
        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.VISIBLE_ELEMENTS_TREE,
            data=json.dumps(scraped_page.element_tree, indent=2).encode(),
        )
        await app.ARTIFACT_MANAGER.create_artifact(
            step=step,
            artifact_type=ArtifactType.VISIBLE_ELEMENTS_TREE_TRIMMED,
            data=json.dumps(scraped_page.element_tree_trimmed, indent=2).encode(),
        )

        return scraped_page, extract_action_prompt

    async def get_extracted_information_for_task(self, task: Task) -> dict[str, Any] | list | str | None:
        """
        Find the last successful ScrapeAction for the task and return the extracted information.
        """
        steps = await app.DATABASE.get_task_steps(
            task_id=task.task_id,
            organization_id=task.organization_id,
        )
        for step in reversed(steps):
            if step.status != StepStatus.completed:
                continue
            if not step.output or not step.output.actions_and_results:
                continue
            for action, action_results in step.output.actions_and_results:
                if action.action_type != ActionType.COMPLETE:
                    continue

                for action_result in action_results:
                    if action_result.success:
                        LOG.info(
                            "Extracted information for task",
                            task_id=task.task_id,
                            step_id=step.step_id,
                            extracted_information=action_result.data,
                        )
                        return action_result.data

        LOG.warning(
            "Failed to find extracted information for task",
            task_id=task.task_id,
        )
        return None

    async def get_failure_reason_for_task(self, task: Task) -> str | None:
        """
        Find the TerminateAction for the task and return the reasoning.
        # TODO (kerem): Also return meaningful exceptions when we add them [WYV-311]
        """
        steps = await app.DATABASE.get_task_steps(
            task_id=task.task_id,
            organization_id=task.organization_id,
        )
        for step in reversed(steps):
            if step.status != StepStatus.completed:
                continue
            if not step.output:
                continue

            if step.output.actions_and_results:
                for action, action_results in step.output.actions_and_results:
                    if action.action_type == ActionType.TERMINATE:
                        return action.reasoning

        LOG.error(
            "Failed to find failure reasoning for task",
            task_id=task.task_id,
        )
        return None

    async def send_task_response(
        self,
        task: Task,
        last_step: Step,
        api_key: str | None = None,
        close_browser_on_completion: bool = True,
    ) -> None:
        """
        send the task response to the webhook callback url
        """
        # refresh the task from the db to get the latest status
        try:
            refreshed_task = await app.DATABASE.get_task(task_id=task.task_id, organization_id=task.organization_id)
            if not refreshed_task:
                LOG.error("Failed to get task from db when sending task response")
                raise TaskNotFound(task_id=task.task_id)
        except Exception as e:
            LOG.error("Failed to get task from db when sending task response", task_id=task.task_id, error=e)
            raise TaskNotFound(task_id=task.task_id) from e
        task = refreshed_task
        # log the task status as an event
        analytics.capture("skyvern-oss-agent-task-status", {"status": task.status})
        # Take one last screenshot and create an artifact before closing the browser to see the final state
        browser_state: BrowserState = await app.BROWSER_MANAGER.get_or_create_for_task(task)
        await browser_state.get_or_create_page()
        try:
            screenshot = await browser_state.take_screenshot(full_page=True)
            await app.ARTIFACT_MANAGER.create_artifact(
                step=last_step,
                artifact_type=ArtifactType.SCREENSHOT_FINAL,
                data=screenshot,
            )
        except TargetClosedError as e:
            LOG.warning(
                "Failed to take screenshot before sending task response, page is closed",
                task_id=task.task_id,
                step_id=last_step.step_id,
                error=e,
            )

        if task.workflow_run_id:
            LOG.info(
                "Task is part of a workflow run, not sending a webhook response",
                task_id=task.task_id,
                workflow_run_id=task.workflow_run_id,
            )
            return

        await self.cleanup_browser_and_create_artifacts(close_browser_on_completion, last_step, task)

        # Wait for all tasks to complete before generating the links for the artifacts
        await app.ARTIFACT_MANAGER.wait_for_upload_aiotasks_for_task(task.task_id)

        await self.execute_task_webhook(task=task, last_step=last_step, api_key=api_key)

    async def execute_task_webhook(self, task: Task, last_step: Step, api_key: str | None) -> None:
        if not api_key:
            LOG.warning(
                "Request has no api key. Not sending task response",
                task_id=task.task_id,
            )
            return

        if not task.webhook_callback_url:
            LOG.warning(
                "Task has no webhook callback url. Not sending task response",
                task_id=task.task_id,
            )
            return

        # get the artifact of the screenshot and get the screenshot_url
        screenshot_artifact = await app.DATABASE.get_artifact(
            task_id=task.task_id,
            step_id=last_step.step_id,
            artifact_type=ArtifactType.SCREENSHOT_FINAL,
            organization_id=task.organization_id,
        )
        screenshot_url = None
        if screenshot_artifact:
            screenshot_url = await app.ARTIFACT_MANAGER.get_share_link(screenshot_artifact)

        recording_artifact = await app.DATABASE.get_artifact(
            task_id=task.task_id,
            step_id=last_step.step_id,
            artifact_type=ArtifactType.RECORDING,
            organization_id=task.organization_id,
        )
        recording_url = None
        if recording_artifact:
            recording_url = await app.ARTIFACT_MANAGER.get_share_link(recording_artifact)

        # get the latest task from the db to get the latest status, extracted_information, and failure_reason
        task_from_db = await app.DATABASE.get_task(task_id=task.task_id, organization_id=task.organization_id)
        if not task_from_db:
            LOG.error("Failed to get task from db when sending task response")
            raise TaskNotFound(task_id=task.task_id)

        task = task_from_db
        if not task.webhook_callback_url:
            LOG.info("Task has no webhook callback url. Not sending task response")
            return

        task_response = task.to_task_response(screenshot_url=screenshot_url, recording_url=recording_url)

        # send task_response to the webhook callback url
        # TODO: use async requests (httpx)
        timestamp = str(int(datetime.utcnow().timestamp()))
        payload = task_response.model_dump_json(exclude={"request": {"navigation_payload"}})
        signature = generate_skyvern_signature(
            payload=payload,
            api_key=api_key,
        )
        headers = {
            "x-skyvern-timestamp": timestamp,
            "x-skyvern-signature": signature,
            "Content-Type": "application/json",
        }
        LOG.info(
            "Sending task response to webhook callback url",
            task_id=task.task_id,
            webhook_callback_url=task.webhook_callback_url,
            payload=payload,
            headers=headers,
        )
        try:
            resp = requests.post(task.webhook_callback_url, data=payload, headers=headers)
            if resp.ok:
                LOG.info(
                    "Webhook sent successfully",
                    task_id=task.task_id,
                    resp_code=resp.status_code,
                    resp_text=resp.text,
                )
            else:
                LOG.info(
                    "Webhook failed",
                    task_id=task.task_id,
                    resp=resp,
                    resp_code=resp.status_code,
                    resp_json=resp.json(),
                    resp_text=resp.text,
                )
        except Exception as e:
            raise FailedToSendWebhook(task_id=task.task_id) from e

    async def cleanup_browser_and_create_artifacts(
        self, close_browser_on_completion: bool, last_step: Step, task: Task
    ) -> None:
        # We need to close the browser even if there is no webhook callback url or api key
        browser_state = await app.BROWSER_MANAGER.cleanup_for_task(task.task_id, close_browser_on_completion)
        if browser_state:
            # Update recording artifact after closing the browser, so we can get an accurate recording
            video_data = await app.BROWSER_MANAGER.get_video_data(task_id=task.task_id, browser_state=browser_state)
            if video_data:
                await app.ARTIFACT_MANAGER.update_artifact_data(
                    artifact_id=browser_state.browser_artifacts.video_artifact_id,
                    organization_id=task.organization_id,
                    data=video_data,
                )

            har_data = await app.BROWSER_MANAGER.get_har_data(task_id=task.task_id, browser_state=browser_state)
            if har_data:
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=last_step,
                    artifact_type=ArtifactType.HAR,
                    data=har_data,
                )

            if browser_state.browser_context and browser_state.browser_artifacts.traces_dir:
                trace_path = f"{browser_state.browser_artifacts.traces_dir}/{task.task_id}.zip"
                await app.ARTIFACT_MANAGER.create_artifact(
                    step=last_step,
                    artifact_type=ArtifactType.TRACE,
                    path=trace_path,
                )
        else:
            LOG.warning(
                "BrowserState is missing before sending response to webhook_callback_url",
                web_hook_url=task.webhook_callback_url,
            )

    async def update_step(
        self,
        step: Step,
        status: StepStatus | None = None,
        output: AgentStepOutput | None = None,
        is_last: bool | None = None,
        retry_index: int | None = None,
    ) -> Step:
        step.validate_update(status, output, is_last)
        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status
        if output is not None:
            updates["output"] = output
        if is_last is not None:
            updates["is_last"] = is_last
        if retry_index is not None:
            updates["retry_index"] = retry_index
        update_comparison = {
            key: {"old": getattr(step, key), "new": value}
            for key, value in updates.items()
            if getattr(step, key) != value
        }
        LOG.info(
            "Updating step in db",
            task_id=step.task_id,
            step_id=step.step_id,
            diff=update_comparison,
        )
        return await app.DATABASE.update_step(
            task_id=step.task_id,
            step_id=step.step_id,
            organization_id=step.organization_id,
            **updates,
        )

    async def update_task(
        self,
        task: Task,
        status: TaskStatus,
        extracted_information: dict[str, Any] | list | str | None = None,
        failure_reason: str | None = None,
    ) -> Task:
        task.validate_update(status, extracted_information, failure_reason)
        updates: dict[str, Any] = {}
        if status is not None:
            updates["status"] = status
        if extracted_information is not None:
            updates["extracted_information"] = extracted_information
        if failure_reason is not None:
            updates["failure_reason"] = failure_reason
        update_comparison = {
            key: {"old": getattr(task, key), "new": value}
            for key, value in updates.items()
            if getattr(task, key) != value
        }
        LOG.info("Updating task in db", task_id=task.task_id, diff=update_comparison)
        return await app.DATABASE.update_task(
            task.task_id,
            organization_id=task.organization_id,
            **updates,
        )

    async def handle_failed_step(self, task: Task, step: Step) -> Step | None:
        if step.retry_index >= SettingsManager.get_settings().MAX_RETRIES_PER_STEP:
            LOG.warning(
                "Step failed after max retries, marking task as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                max_retries=SettingsManager.get_settings().MAX_RETRIES_PER_STEP,
            )
            await self.update_task(
                task,
                TaskStatus.failed,
                failure_reason=f"Max retries per step ({SettingsManager.get_settings().MAX_RETRIES_PER_STEP}) exceeded",
            )
            return None
        else:
            LOG.warning(
                "Step failed, retrying",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
            )
            next_step = await app.DATABASE.create_step(
                task_id=task.task_id,
                organization_id=task.organization_id,
                order=step.order,
                retry_index=step.retry_index + 1,
            )
            return next_step

    async def handle_completed_step(
        self, organization: Organization, task: Task, step: Step
    ) -> tuple[bool | None, Step | None, Step | None]:
        if step.is_goal_achieved():
            LOG.info(
                "Step completed and goal achieved, marking task as completed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                output=step.output,
            )
            last_step = await self.update_step(step, is_last=True)
            extracted_information = await self.get_extracted_information_for_task(task)
            await self.update_task(task, status=TaskStatus.completed, extracted_information=extracted_information)
            return True, last_step, None
        if step.is_terminated():
            LOG.info(
                "Step completed and terminated by the agent, marking task as terminated",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                output=step.output,
            )
            last_step = await self.update_step(step, is_last=True)
            failure_reason = await self.get_failure_reason_for_task(task)
            await self.update_task(task, status=TaskStatus.terminated, failure_reason=failure_reason)
            return False, last_step, None
        # If the max steps are exceeded, mark the current step as the last step and conclude the task
        context = skyvern_context.ensure_context()
        override_max_steps_per_run = context.max_steps_override
        max_steps_per_run = (
            override_max_steps_per_run
            or organization.max_steps_per_run
            or SettingsManager.get_settings().MAX_STEPS_PER_RUN
        )
        if step.order + 1 >= max_steps_per_run:
            LOG.info(
                "Step completed but max steps reached, marking task as failed",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                output=step.output,
                max_steps=max_steps_per_run,
            )
            last_step = await self.update_step(step, is_last=True)
            await self.update_task(
                task,
                status=TaskStatus.failed,
                failure_reason=f"Max steps per task ({max_steps_per_run}) exceeded",
            )
            return False, last_step, None
        else:
            LOG.info(
                "Step completed, creating next step",
                task_id=task.task_id,
                step_id=step.step_id,
                step_order=step.order,
                step_retry=step.retry_index,
                output=step.output,
            )
            next_step = await app.DATABASE.create_step(
                task_id=task.task_id,
                order=step.order + 1,
                retry_index=0,
                organization_id=task.organization_id,
            )

            if step.order == int(
                max_steps_per_run * SettingsManager.get_settings().LONG_RUNNING_TASK_WARNING_RATIO - 1
            ):
                LOG.info(
                    "Long running task warning",
                    order=step.order,
                    max_steps=max_steps_per_run,
                    warning_ratio=SettingsManager.get_settings().LONG_RUNNING_TASK_WARNING_RATIO,
                )
            return None, None, next_step

    @staticmethod
    async def get_task_errors(task: Task) -> list[UserDefinedError]:
        steps = await app.DATABASE.get_task_steps(task_id=task.task_id, organization_id=task.organization_id)
        errors = []
        for step in steps:
            if step.output and step.output.errors:
                errors.extend(step.output.errors)

        return errors

    @staticmethod
    async def update_task_errors_from_detailed_output(
        task: Task, detailed_step_output: DetailedAgentStepOutput
    ) -> Task:
        task_errors = task.errors
        step_errors = detailed_step_output.extract_errors() or []
        task_errors.extend([error.model_dump() for error in step_errors])

        return await app.DATABASE.update_task(
            task_id=task.task_id, organization_id=task.organization_id, errors=task_errors
        )
