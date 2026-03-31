from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from shared.utils.logging_config import get_logger

logger = get_logger("agent-core-loop")


class AgentLoop:
    """Orchestrates the Observe→Understand→Reason→Plan→Act→Verify→Log→Learn loop."""

    def __init__(
        self,
        task_id: str,
        goal: str,
        user_id: str,
        service_urls: dict[str, str],
    ) -> None:
        self.task_id = task_id
        self.goal = goal
        self.user_id = user_id
        self.service_urls = service_urls
        self.client = httpx.AsyncClient(timeout=30.0)
        self.step_counter = 0
        self.memory: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Phase helpers                                                        #
    # ------------------------------------------------------------------ #

    async def observe(self) -> dict[str, Any]:
        """Capture the current screen via the screen-capture service."""
        url = f"{self.service_urls['screen_capture']}/capture"
        try:
            response = await self.client.get(url)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            logger.info("observe completed", extra={"task_id": self.task_id, "step": self.step_counter})
            return data
        except httpx.HTTPError as exc:
            logger.error("observe failed", extra={"task_id": self.task_id, "error": str(exc)})
            raise

    async def understand(self, screenshot_b64: str) -> dict[str, Any]:
        """Extract text and UI elements from the screenshot via the vision service."""
        url = f"{self.service_urls['vision']}/analyze"
        payload = {"screenshot_b64": screenshot_b64}
        try:
            response = await self.client.post(url, json=payload)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            logger.info(
                "understand completed",
                extra={
                    "task_id": self.task_id,
                    "step": self.step_counter,
                    "element_count": len(data.get("detected_elements", [])),
                },
            )
            return data
        except httpx.HTTPError as exc:
            logger.error("understand failed", extra={"task_id": self.task_id, "error": str(exc)})
            raise

    async def reason(self, screen_state: dict[str, Any]) -> dict[str, Any]:
        """Ask the LLM-reasoning service what action to take next."""
        url = f"{self.service_urls['llm_reasoning']}/reason"
        payload = {**screen_state, "goal": self.goal, "task_id": self.task_id, "step": self.step_counter}
        try:
            response = await self.client.post(url, json=payload)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            logger.info(
                "reason completed",
                extra={
                    "task_id": self.task_id,
                    "step": self.step_counter,
                    "decision": data.get("decision"),
                    "confidence": data.get("confidence"),
                },
            )
            return data
        except httpx.HTTPError as exc:
            logger.error("reason failed", extra={"task_id": self.task_id, "error": str(exc)})
            raise

    async def plan(self, reasoning: dict[str, Any]) -> list[dict[str, Any]]:
        """Break the next decision into ordered steps via the task-planner service."""
        url = f"{self.service_urls['task_planner']}/plan"
        payload = {"reasoning": reasoning, "goal": self.goal, "task_id": self.task_id}
        try:
            response = await self.client.post(url, json=payload)
            response.raise_for_status()
            steps: list[dict[str, Any]] = response.json()
            logger.info(
                "plan created",
                extra={"task_id": self.task_id, "step": self.step_counter, "plan_steps": len(steps)},
            )
            return steps
        except httpx.HTTPError as exc:
            logger.error("plan failed", extra={"task_id": self.task_id, "error": str(exc)})
            raise

    async def act(self, action: dict[str, Any]) -> dict[str, Any]:
        """Execute a single action via the action-execution service."""
        url = f"{self.service_urls['action_execution']}/execute"
        payload = {"action": action, "task_id": self.task_id}
        try:
            response = await self.client.post(url, json=payload)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            logger.info(
                "act completed",
                extra={
                    "task_id": self.task_id,
                    "step": self.step_counter,
                    "action_type": action.get("action_type"),
                    "success": data.get("success"),
                },
            )
            return data
        except httpx.HTTPError as exc:
            logger.error("act failed", extra={"task_id": self.task_id, "error": str(exc)})
            raise

    async def verify(self, expected_outcome: str) -> dict[str, Any]:
        """Capture a fresh screenshot and verify the expected outcome was reached."""
        observation = await self.observe()
        url = f"{self.service_urls['verification']}/verify"
        payload = {
            "screenshot_b64": observation.get("screenshot_b64", ""),
            "expected_outcome": expected_outcome,
            "task_id": self.task_id,
            "step": self.step_counter,
        }
        try:
            response = await self.client.post(url, json=payload)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            logger.info(
                "verify completed",
                extra={
                    "task_id": self.task_id,
                    "step": self.step_counter,
                    "verified": data.get("verified"),
                },
            )
            return data
        except httpx.HTTPError as exc:
            logger.error("verify failed", extra={"task_id": self.task_id, "error": str(exc)})
            raise

    async def log_step(self, step_data: dict[str, Any]) -> None:
        """Persist structured step data to the observability service."""
        url = f"{self.service_urls['observability']}/log"
        payload = {
            **step_data,
            "task_id": self.task_id,
            "step": self.step_counter,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            response = await self.client.post(url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("log_step failed (non-fatal)", extra={"task_id": self.task_id, "error": str(exc)})

    async def learn(self, step_data: dict[str, Any]) -> None:
        """Store experience in the memory service for future retrieval."""
        url = f"{self.service_urls['memory']}/store"
        content = (
            f"Step {self.step_counter}: decision={step_data.get('decision')} "
            f"success={step_data.get('action_success')} goal={self.goal}"
        )
        payload = {
            "task_id": self.task_id,
            "content": content,
            "importance_score": step_data.get("confidence", 0.5),
            "memory_type": "experience",
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            response = await self.client.post(url, json=payload)
            response.raise_for_status()
            entry = response.json()
            self.memory.append(entry)
        except httpx.HTTPError as exc:
            logger.warning("learn failed (non-fatal)", extra={"task_id": self.task_id, "error": str(exc)})

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #

    async def run(self, max_steps: int = 50) -> dict[str, Any]:
        """Execute the full agent loop until the task completes or max_steps is reached."""
        start_time = time.monotonic()
        final_status = "failed"

        logger.info(
            "agent loop started",
            extra={"task_id": self.task_id, "goal": self.goal, "max_steps": max_steps},
        )

        try:
            for _ in range(max_steps):
                self.step_counter += 1
                step_start = time.monotonic()

                # 1. Observe
                observation = await self.observe()
                screenshot_b64 = observation.get("screenshot_b64", "")

                # 2. Understand
                screen_state = await self.understand(screenshot_b64)
                screen_state["screenshot_b64"] = screenshot_b64
                screen_state.setdefault("timestamp", observation.get("timestamp", ""))

                # 3. Reason
                reasoning = await self.reason(screen_state)
                decision = reasoning.get("decision", "")
                confidence = reasoning.get("confidence", 0.0)
                next_action: dict[str, Any] = reasoning.get("next_action", {})
                expected_outcome: str = reasoning.get("expected_outcome") or reasoning.get("decision", "")

                # 4. Plan – expand the next action into ordered sub-steps, then execute the first
                planned_steps = await self.plan(reasoning)
                if planned_steps:
                    next_action = planned_steps[0].get("action", next_action)

                # 5. Act
                action_result = await self.act(next_action)
                action_success = action_result.get("success", False)

                # 6. Verify
                verification = await self.verify(expected_outcome)
                verified = verification.get("verified", False)

                step_time = time.monotonic() - step_start

                step_data: dict[str, Any] = {
                    "step": self.step_counter,
                    "decision": decision,
                    "confidence": confidence,
                    "action": next_action,
                    "action_success": action_success,
                    "verified": verified,
                    "step_time": step_time,
                    "ocr_text": screen_state.get("ocr_text", ""),
                    "detected_elements": screen_state.get("detected_elements", []),
                    "reason": reasoning.get("reason", ""),
                    "expected_outcome": expected_outcome,
                    "alternatives": reasoning.get("alternatives", []),
                }

                # 6. Log
                await self.log_step(step_data)

                # 7. Learn
                await self.learn(step_data)

                # 8. Check for completion
                if decision == "TASK_COMPLETE":
                    final_status = "completed"
                    logger.info(
                        "task completed",
                        extra={"task_id": self.task_id, "steps_taken": self.step_counter},
                    )
                    break

                if not action_success:
                    logger.warning(
                        "action unsuccessful, continuing",
                        extra={"task_id": self.task_id, "step": self.step_counter},
                    )
            else:
                logger.warning(
                    "max_steps reached without completion",
                    extra={"task_id": self.task_id, "max_steps": max_steps},
                )
                final_status = "failed"

        except Exception as exc:
            logger.error(
                "agent loop error",
                extra={"task_id": self.task_id, "step": self.step_counter, "error": str(exc)},
            )
            final_status = "failed"
            raise
        finally:
            await self.client.aclose()

        total_time = time.monotonic() - start_time

        summary: dict[str, Any] = {
            "task_id": self.task_id,
            "steps_taken": self.step_counter,
            "final_status": final_status,
            "total_time": round(total_time, 3),
        }
        logger.info("agent loop finished", extra=summary)
        return summary
