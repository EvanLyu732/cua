"""
ComputerAgent - Main agent class that selects and runs agent loops
"""

import asyncio
from typing import Dict, List, Any, Optional, AsyncGenerator, Union, cast, Callable, Set

from litellm.responses.utils import Usage

from .types import Messages, Computer
from .decorators import find_agent_loop
from .computer_handler import OpenAIComputerHandler, acknowledge_safety_check_callback, check_blocklisted_url
import json
import litellm
import litellm.utils
import inspect
from .adapters import HuggingFaceLocalAdapter
from .callbacks import (
    ImageRetentionCallback, 
    LoggingCallback, 
    TrajectorySaverCallback, 
    BudgetManagerCallback,
    TelemetryCallback,
)

def get_json(obj: Any, max_depth: int = 10) -> Any:
    def custom_serializer(o: Any, depth: int = 0, seen: Set[int] = None) -> Any:
        if seen is None:
            seen = set()
        
        # Use model_dump() if available
        if hasattr(o, 'model_dump'):
            return o.model_dump()
        
        # Check depth limit
        if depth > max_depth:
            return f"<max_depth_exceeded:{max_depth}>"
        
        # Check for circular references using object id
        obj_id = id(o)
        if obj_id in seen:
            return f"<circular_reference:{type(o).__name__}>"
        
        # Handle Computer objects
        if hasattr(o, '__class__') and 'computer' in getattr(o, '__class__').__name__.lower():
            return f"<computer:{o.__class__.__name__}>"

        # Handle objects with __dict__
        if hasattr(o, '__dict__'):
            seen.add(obj_id)
            try:
                result = {}
                for k, v in o.__dict__.items():
                    if v is not None:
                        # Recursively serialize with updated depth and seen set
                        serialized_value = custom_serializer(v, depth + 1, seen.copy())
                        result[k] = serialized_value
                return result
            finally:
                seen.discard(obj_id)
        
        # Handle common types that might contain nested objects
        elif isinstance(o, dict):
            seen.add(obj_id)
            try:
                return {
                    k: custom_serializer(v, depth + 1, seen.copy())
                    for k, v in o.items()
                    if v is not None
                }
            finally:
                seen.discard(obj_id)
        
        elif isinstance(o, (list, tuple, set)):
            seen.add(obj_id)
            try:
                return [
                    custom_serializer(item, depth + 1, seen.copy())
                    for item in o
                    if item is not None
                ]
            finally:
                seen.discard(obj_id)
        
        # For basic types that json.dumps can handle
        elif isinstance(o, (str, int, float, bool)) or o is None:
            return o
        
        # Fallback to string representation
        else:
            return str(o)
    
    def remove_nones(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: remove_nones(v) for k, v in obj.items() if v is not None}
        elif isinstance(obj, list):
            return [remove_nones(item) for item in obj if item is not None]
        return obj
    
    # Serialize with circular reference and depth protection
    serialized = custom_serializer(obj)
    
    # Convert to JSON string and back to ensure JSON compatibility
    json_str = json.dumps(serialized)
    parsed = json.loads(json_str)
    
    # Final cleanup of any remaining None values
    return remove_nones(parsed)

def sanitize_message(msg: Any) -> Any:
    """Return a copy of the message with image_url omitted for computer_call_output messages."""
    if msg.get("type") == "computer_call_output":
        output = msg.get("output", {})
        if isinstance(output, dict):
            sanitized = msg.copy()
            sanitized["output"] = {**output, "image_url": "[omitted]"}
            return sanitized
    return msg

class ComputerAgent:
    """
    Main agent class that automatically selects the appropriate agent loop
    based on the model and executes tool calls.
    """
    
    def __init__(
        self,
        model: str,
        tools: Optional[List[Any]] = None,
        custom_loop: Optional[Callable] = None,
        only_n_most_recent_images: Optional[int] = None,
        callbacks: Optional[List[Any]] = None,
        verbosity: Optional[int] = None,
        trajectory_dir: Optional[str] = None,
        max_retries: Optional[int] = 3,
        screenshot_delay: Optional[float | int] = 0.5,
        use_prompt_caching: Optional[bool] = False,
        max_trajectory_budget: Optional[float | dict] = None,
        telemetry_enabled: Optional[bool] = True,
        **kwargs
    ):
        """
        Initialize ComputerAgent.
        
        Args:
            model: Model name (e.g., "claude-3-5-sonnet-20241022", "computer-use-preview", "omni+vertex_ai/gemini-pro")
            tools: List of tools (computer objects, decorated functions, etc.)
            custom_loop: Custom agent loop function to use instead of auto-selection
            only_n_most_recent_images: If set, only keep the N most recent images in message history. Adds ImageRetentionCallback automatically.
            callbacks: List of AsyncCallbackHandler instances for preprocessing/postprocessing
            verbosity: Logging level (logging.DEBUG, logging.INFO, etc.). If set, adds LoggingCallback automatically
            trajectory_dir: If set, saves trajectory data (screenshots, responses) to this directory. Adds TrajectorySaverCallback automatically.
            max_retries: Maximum number of retries for failed API calls
            screenshot_delay: Delay before screenshots in seconds
            use_prompt_caching: If set, use prompt caching to avoid reprocessing the same prompt. Intended for use with anthropic providers.
            max_trajectory_budget: If set, adds BudgetManagerCallback to track usage costs and stop when budget is exceeded
            telemetry_enabled: If set, adds TelemetryCallback to track anonymized usage data. Enabled by default.
            **kwargs: Additional arguments passed to the agent loop
        """
        self.model = model
        self.tools = tools or []
        self.custom_loop = custom_loop
        self.only_n_most_recent_images = only_n_most_recent_images
        self.callbacks = callbacks or []
        self.verbosity = verbosity
        self.trajectory_dir = trajectory_dir
        self.max_retries = max_retries
        self.screenshot_delay = screenshot_delay
        self.use_prompt_caching = use_prompt_caching
        self.telemetry_enabled = telemetry_enabled
        self.kwargs = kwargs

        # == Add built-in callbacks ==

        # Add telemetry callback if telemetry_enabled is set
        if self.telemetry_enabled:
            if isinstance(self.telemetry_enabled, bool):
                self.callbacks.append(TelemetryCallback(self))
            else:
                self.callbacks.append(TelemetryCallback(self, **self.telemetry_enabled))

        # Add logging callback if verbosity is set
        if self.verbosity is not None:
            self.callbacks.append(LoggingCallback(level=self.verbosity))

        # Add image retention callback if only_n_most_recent_images is set
        if self.only_n_most_recent_images:
            self.callbacks.append(ImageRetentionCallback(self.only_n_most_recent_images))
        
        # Add trajectory saver callback if trajectory_dir is set
        if self.trajectory_dir:
            self.callbacks.append(TrajectorySaverCallback(self.trajectory_dir))
        
        # Add budget manager if max_trajectory_budget is set
        if max_trajectory_budget:
            if isinstance(max_trajectory_budget, dict):
                self.callbacks.append(BudgetManagerCallback(**max_trajectory_budget))
            else:
                self.callbacks.append(BudgetManagerCallback(max_trajectory_budget))
        
        # == Enable local model providers w/ LiteLLM ==

        # Register local model providers
        hf_adapter = HuggingFaceLocalAdapter(
            device="auto"
        )
        litellm.custom_provider_map = [
            {"provider": "huggingface-local", "custom_handler": hf_adapter}
        ]

        # == Initialize computer agent ==

        # Find the appropriate agent loop
        if custom_loop:
            self.agent_loop = custom_loop
            self.agent_loop_info = None
        else:
            loop_info = find_agent_loop(model)
            if not loop_info:
                raise ValueError(f"No agent loop found for model: {model}")
            self.agent_loop = loop_info.func
            self.agent_loop_info = loop_info
        
        self.tool_schemas = []
        self.computer_handler = None
        
    async def _initialize_computers(self):
        """Initialize computer objects"""
        if not self.tool_schemas:
            for tool in self.tools:
                if hasattr(tool, '_initialized') and not tool._initialized:
                    await tool.run()
                
            # Process tools and create tool schemas
            self.tool_schemas = self._process_tools()
            
            # Find computer tool and create interface adapter
            computer_handler = None
            for schema in self.tool_schemas:
                if schema["type"] == "computer":
                    computer_handler = OpenAIComputerHandler(schema["computer"].interface)
                    break
            self.computer_handler = computer_handler
    
    def _process_input(self, input: Messages) -> List[Dict[str, Any]]:
        """Process input messages and create schemas for the agent loop"""
        if isinstance(input, str):
            return [{"role": "user", "content": input}]
        return [get_json(msg) for msg in input]

    def _process_tools(self) -> List[Dict[str, Any]]:
        """Process tools and create schemas for the agent loop"""
        schemas = []
        
        for tool in self.tools:
            # Check if it's a computer object (has interface attribute)
            if hasattr(tool, 'interface'):
                # This is a computer tool - will be handled by agent loop
                schemas.append({
                    "type": "computer",
                    "computer": tool
                })
            elif callable(tool):
                # Use litellm.utils.function_to_dict to extract schema from docstring
                try:
                    function_schema = litellm.utils.function_to_dict(tool)
                    schemas.append({
                        "type": "function",
                        "function": function_schema
                    })
                except Exception as e:
                    print(f"Warning: Could not process tool {tool}: {e}")
            else:
                print(f"Warning: Unknown tool type: {tool}")
        
        return schemas
    
    def _get_tool(self, name: str) -> Optional[Callable]:
        """Get a tool by name"""
        for tool in self.tools:
            if hasattr(tool, '__name__') and tool.__name__ == name:
                return tool
            elif hasattr(tool, 'func') and tool.func.__name__ == name:
                return tool
        return None
    
    # ============================================================================
    # AGENT RUN LOOP LIFECYCLE HOOKS
    # ============================================================================
    
    async def _on_run_start(self, kwargs: Dict[str, Any], old_items: List[Dict[str, Any]]) -> None:
        """Initialize run tracking by calling callbacks."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_run_start'):
                await callback.on_run_start(kwargs, old_items)
    
    async def _on_run_end(self, kwargs: Dict[str, Any], old_items: List[Dict[str, Any]], new_items: List[Dict[str, Any]]) -> None:
        """Finalize run tracking by calling callbacks."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_run_end'):
                await callback.on_run_end(kwargs, old_items, new_items)
    
    async def _on_run_continue(self, kwargs: Dict[str, Any], old_items: List[Dict[str, Any]], new_items: List[Dict[str, Any]]) -> bool:
        """Check if run should continue by calling callbacks."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_run_continue'):
                should_continue = await callback.on_run_continue(kwargs, old_items, new_items)
                if not should_continue:
                    return False
        return True
    
    async def _on_llm_start(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Prepare messages for the LLM call by applying callbacks."""
        result = messages
        for callback in self.callbacks:
            if hasattr(callback, 'on_llm_start'):
                result = await callback.on_llm_start(result)
        return result

    async def _on_llm_end(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Postprocess messages after the LLM call by applying callbacks."""
        result = messages
        for callback in self.callbacks:
            if hasattr(callback, 'on_llm_end'):
                result = await callback.on_llm_end(result)
        return result

    async def _on_responses(self, kwargs: Dict[str, Any], responses: Dict[str, Any]) -> None:
        """Called when responses are received."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_responses'):
                await callback.on_responses(get_json(kwargs), get_json(responses))
    
    async def _on_computer_call_start(self, item: Dict[str, Any]) -> None:
        """Called when a computer call is about to start."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_computer_call_start'):
                await callback.on_computer_call_start(get_json(item))
    
    async def _on_computer_call_end(self, item: Dict[str, Any], result: List[Dict[str, Any]]) -> None:
        """Called when a computer call has completed."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_computer_call_end'):
                await callback.on_computer_call_end(get_json(item), get_json(result))
    
    async def _on_function_call_start(self, item: Dict[str, Any]) -> None:
        """Called when a function call is about to start."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_function_call_start'):
                await callback.on_function_call_start(get_json(item))
    
    async def _on_function_call_end(self, item: Dict[str, Any], result: List[Dict[str, Any]]) -> None:
        """Called when a function call has completed."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_function_call_end'):
                await callback.on_function_call_end(get_json(item), get_json(result))
    
    async def _on_text(self, item: Dict[str, Any]) -> None:
        """Called when a text message is encountered."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_text'):
                await callback.on_text(get_json(item))
    
    async def _on_api_start(self, kwargs: Dict[str, Any]) -> None:
        """Called when an LLM API call is about to start."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_api_start'):
                await callback.on_api_start(get_json(kwargs))
    
    async def _on_api_end(self, kwargs: Dict[str, Any], result: Any) -> None:
        """Called when an LLM API call has completed."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_api_end'):
                await callback.on_api_end(get_json(kwargs), get_json(result))

    async def _on_usage(self, usage: Dict[str, Any]) -> None:
        """Called when usage information is received."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_usage'):
                await callback.on_usage(get_json(usage))

    async def _on_screenshot(self, screenshot: Union[str, bytes], name: str = "screenshot") -> None:
        """Called when a screenshot is taken."""
        for callback in self.callbacks:
            if hasattr(callback, 'on_screenshot'):
                await callback.on_screenshot(screenshot, name)

    # ============================================================================
    # AGENT OUTPUT PROCESSING
    # ============================================================================
    
    async def _handle_item(self, item: Any, computer: Optional[Computer] = None) -> List[Dict[str, Any]]:
        """Handle each item; may cause a computer action + screenshot."""
        
        item_type = item.get("type", None)
        
        if item_type == "message":
            await self._on_text(item)
            # # Print messages
            # if item.get("content"):
            #     for content_item in item.get("content"):
            #         if content_item.get("text"):
            #             print(content_item.get("text"))
            return []
        
        if item_type == "computer_call":
            await self._on_computer_call_start(item)
            if not computer:
                raise ValueError("Computer handler is required for computer calls")

            # Perform computer actions
            action = item.get("action")
            action_type = action.get("type")
            
            # Extract action arguments (all fields except 'type')
            action_args = {k: v for k, v in action.items() if k != "type"}
            
            # print(f"{action_type}({action_args})")
            
            # Execute the computer action
            computer_method = getattr(computer, action_type, None)
            if computer_method:
                await computer_method(**action_args)
            else:
                print(f"Unknown computer action: {action_type}")
                return []
            
            # Take screenshot after action
            if self.screenshot_delay and self.screenshot_delay > 0:
                await asyncio.sleep(self.screenshot_delay)
            screenshot_base64 = await computer.screenshot()
            await self._on_screenshot(screenshot_base64, "screenshot_after")
            
            # Handle safety checks
            pending_checks = item.get("pending_safety_checks", [])
            acknowledged_checks = []
            for check in pending_checks:
                check_message = check.get("message", str(check))
                if acknowledge_safety_check_callback(check_message):
                    acknowledged_checks.append(check)
                else:
                    raise ValueError(f"Safety check failed: {check_message}")
            
            # Create call output
            call_output = {
                "type": "computer_call_output",
                "call_id": item.get("call_id"),
                "acknowledged_safety_checks": acknowledged_checks,
                "output": {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{screenshot_base64}",
                },
            }
            
            # Additional URL safety checks for browser environments
            if await computer.get_environment() == "browser":
                current_url = await computer.get_current_url()
                call_output["output"]["current_url"] = current_url
                check_blocklisted_url(current_url)
            
            result = [call_output]
            await self._on_computer_call_end(item, result)
            return result
        
        if item_type == "function_call":
            await self._on_function_call_start(item)
            # Perform function call
            function = self._get_tool(item.get("name"))
            if not function:
                raise ValueError(f"Function {item.get("name")} not found")
        
            args = json.loads(item.get("arguments"))

            # Execute function - use asyncio.to_thread for non-async functions
            if inspect.iscoroutinefunction(function):
                result = await function(**args)
            else:
                result = await asyncio.to_thread(function, **args)
        
            # Create function call output
            call_output = {
                "type": "function_call_output",
                "call_id": item.get("call_id"),
                "output": str(result),
            }
        
            result = [call_output]
            await self._on_function_call_end(item, result)
            return result

        return []

    # ============================================================================
    # MAIN AGENT LOOP
    # ============================================================================
    
    async def run(
        self,
        messages: Messages,
        stream: bool = False,
        **kwargs
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Run the agent with the given messages using Computer protocol handler pattern.
        
        Args:
            messages: List of message dictionaries
            stream: Whether to stream the response
            **kwargs: Additional arguments
            
        Returns:
            AsyncGenerator that yields response chunks
        """

        await self._initialize_computers()
        
        # Merge kwargs
        merged_kwargs = {**self.kwargs, **kwargs}
        
        old_items = self._process_input(messages)
        new_items = []

        # Initialize run tracking
        run_kwargs = {
            "messages": messages,
            "stream": stream,
            "model": self.model,
            "agent_loop": self.agent_loop.__name__,
            **merged_kwargs
        }
        await self._on_run_start(run_kwargs, old_items)

        while new_items[-1].get("role") != "assistant" if new_items else True:
            # Lifecycle hook: Check if we should continue based on callbacks (e.g., budget manager)
            should_continue = await self._on_run_continue(run_kwargs, old_items, new_items)
            if not should_continue:
                break

            # Lifecycle hook: Prepare messages for the LLM call
            # Use cases:
            # - PII anonymization
            # - Image retention policy
            combined_messages = old_items + new_items
            preprocessed_messages = await self._on_llm_start(combined_messages)
            
            loop_kwargs = {
                "messages": preprocessed_messages,
                "model": self.model,
                "tools": self.tool_schemas,
                "stream": False,
                "computer_handler": self.computer_handler,
                "max_retries": self.max_retries,
                "use_prompt_caching": self.use_prompt_caching,
                **merged_kwargs
            }

            # Run agent loop iteration
            result = await self.agent_loop(
                **loop_kwargs,
                _on_api_start=self._on_api_start,
                _on_api_end=self._on_api_end,
                _on_usage=self._on_usage,
                _on_screenshot=self._on_screenshot,
            )
            result = get_json(result)
            
            # Lifecycle hook: Postprocess messages after the LLM call
            # Use cases:
            # - PII deanonymization (if you want tool calls to see PII)
            result["output"] = await self._on_llm_end(result.get("output", []))
            await self._on_responses(loop_kwargs, result)
            
            # Yield agent response
            yield result

            # Add agent response to new_items
            new_items += result.get("output")

            # Handle computer actions
            for item in result.get("output"):
                partial_items = await self._handle_item(item, self.computer_handler)
                new_items += partial_items

                # Yield partial response
                yield {
                    "output": partial_items,
                    "usage": Usage(
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                    )
                }
        
        await self._on_run_end(loop_kwargs, old_items, new_items)