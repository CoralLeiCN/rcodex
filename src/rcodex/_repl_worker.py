"""Restricted persistent Python worker used by :mod:`rcodex.repl`.

The worker has a deliberately tiny dependency surface so it can run with ``python -I -S``.
Protocol messages are newline-delimited JSON on stdin/stdout. Model-authored ``print`` output is
captured and never shares the protocol stream.
"""

from __future__ import annotations

import ast
import io
import json
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

_PROTOCOL_IN = sys.stdin
_PROTOCOL_OUT = sys.stdout


class _UnsafeCode(ValueError):
    pass


class _CodeValidator(ast.NodeVisitor):
    """Reject access paths that can escape the restricted Python namespace."""

    _forbidden = (
        ast.AsyncFunctionDef,
        ast.Await,
        ast.ClassDef,
        ast.Global,
        ast.Import,
        ast.ImportFrom,
        ast.Nonlocal,
    )

    def __init__(self) -> None:
        self.names: set[str] = set()

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, self._forbidden):
            raise _UnsafeCode(f"{type(node).__name__} is unavailable in the rcodex REPL")
        super().generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("_"):
            raise _UnsafeCode("private and dunder attributes are unavailable in the rcodex REPL")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_"):
            raise _UnsafeCode("private and dunder names are unavailable in the rcodex REPL")
        if len(node.id) > 128:
            raise _UnsafeCode("REPL variable names may not exceed 128 characters")
        self.names.add(node.id)
        if len(self.names) > 4096:
            raise _UnsafeCode("one REPL block may not reference more than 4096 names")
        self.generic_visit(node)


class _AnswerDict(dict[str, Any]):
    def __init__(self) -> None:
        super().__init__(content="", ready=False)


def _send(message: dict[str, Any]) -> None:
    _PROTOCOL_OUT.write(
        json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    )
    _PROTOCOL_OUT.flush()


def _receive() -> dict[str, Any]:
    line = _PROTOCOL_IN.readline()
    if not line:
        raise EOFError("REPL controller disconnected")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("REPL protocol message must be an object")
    return value


def _truncate_utf8(value: str, maximum: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value, False
    return encoded[:maximum].decode("utf-8", errors="ignore"), True


class _Context:
    """Lazy RPC proxy; file handles and filesystem access stay in the controller."""

    def __init__(self, request: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._request = request

    def _call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self._request({"type": "context", "arguments": arguments})
        if response.get("error") is not None:
            raise RuntimeError(response["error"])
        value = response.get("value")
        if not isinstance(value, dict):
            raise RuntimeError("invalid controller context response")
        return value

    def files(self) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            page = self._call({"operation": "files", "offset": offset})
            yield from page["entries"]
            if page["eof"]:
                return
            offset = page["next_offset"]

    def read(self, entry_id: str, offset: int = 0, max_bytes: int = 65536) -> dict[str, Any]:
        return self._call(
            {"operation": "read", "entry_id": entry_id, "offset": offset, "max_bytes": max_bytes}
        )

    def chunks(self, entry_id: str, max_bytes: int = 65536) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            chunk = self.read(entry_id, offset, max_bytes)
            yield chunk
            if chunk["eof"]:
                return
            offset = chunk["next_offset"]


class _Worker:
    def __init__(self, tool_names: list[str], max_output_bytes: int) -> None:
        self.max_output_bytes = max_output_bytes
        self.answer = _AnswerDict()
        self.protected: dict[str, Any] = {
            "context": _Context(self._request),
            "llm_query": self.llm_query,
            "llm_query_batched": self.llm_query_batched,
            "rlm_query": self.rlm_query,
            "rlm_query_batched": self.rlm_query_batched,
            "SHOW_VARS": self.show_vars,
            "submit_answer": self.submit_answer,
            "answer": self.answer,
        }
        for name in tool_names:
            self.protected[name] = self._tool_proxy(name)
        self.namespace: dict[str, Any] = {
            "__builtins__": {
                "ArithmeticError": ArithmeticError,
                "AssertionError": AssertionError,
                "Exception": Exception,
                "IndexError": IndexError,
                "KeyError": KeyError,
                "LookupError": LookupError,
                "NameError": NameError,
                "RuntimeError": RuntimeError,
                "StopIteration": StopIteration,
                "TypeError": TypeError,
                "ValueError": ValueError,
                "abs": abs,
                "all": all,
                "any": any,
                "ascii": ascii,
                "bin": bin,
                "bool": bool,
                "bytearray": bytearray,
                "bytes": bytes,
                "chr": chr,
                "complex": complex,
                "dict": dict,
                "divmod": divmod,
                "enumerate": enumerate,
                "filter": filter,
                "float": float,
                "format": format,
                "frozenset": frozenset,
                "hex": hex,
                "int": int,
                "isinstance": isinstance,
                "issubclass": issubclass,
                "iter": iter,
                "len": len,
                "list": list,
                "map": map,
                "max": max,
                "memoryview": memoryview,
                "min": min,
                "next": next,
                "oct": oct,
                "ord": ord,
                "pow": pow,
                "print": print,
                "range": range,
                "repr": repr,
                "reversed": reversed,
                "round": round,
                "set": set,
                "slice": slice,
                "sorted": sorted,
                "str": str,
                "sum": sum,
                "tuple": tuple,
                "zip": zip,
            },
            "__name__": "__main__",
            **self.protected,
        }

    def _request(self, message: dict[str, Any]) -> dict[str, Any]:
        request_id = f"rpc_{uuid.uuid4().hex}"
        _send({**message, "request_id": request_id})
        response = _receive()
        if response.get("type") != "rpc_result" or response.get("request_id") != request_id:
            raise RuntimeError("REPL controller returned an invalid RPC response")
        return response

    def _query(self, mode: str, prompts: list[str], model: str | None) -> list[str]:
        if not all(isinstance(prompt, str) and prompt.strip() for prompt in prompts):
            raise ValueError("query prompts must be non-blank strings")
        if model is not None and not isinstance(model, str):
            raise TypeError("model must be a string or None")
        response = self._request(
            {"type": "query", "mode": mode, "prompts": prompts, "model": model}
        )
        values = response.get("values")
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise RuntimeError("REPL controller returned invalid query values")
        return values

    def llm_query(self, prompt: str, model: str | None = None) -> str:
        return self._query("leaf", [prompt], model)[0]

    def llm_query_batched(self, prompts: list[str], model: str | None = None) -> list[str]:
        return self._query("leaf", list(prompts), model)

    def rlm_query(self, prompt: str, model: str | None = None) -> str:
        return self._query("recursive", [prompt], model)[0]

    def rlm_query_batched(self, prompts: list[str], model: str | None = None) -> list[str]:
        return self._query("recursive", list(prompts), model)

    def _tool_proxy(self, name: str) -> Any:
        def invoke(*args: Any, **kwargs: Any) -> Any:
            if args:
                if len(args) != 1 or kwargs or not isinstance(args[0], dict):
                    raise TypeError(f"{name} accepts keyword arguments or one argument dictionary")
                arguments = args[0]
            else:
                arguments = kwargs
            response = self._request({"type": "tool", "name": name, "arguments": arguments})
            if response.get("error") is not None:
                return f"Error: {response['error']}"
            return response.get("value")

        return invoke

    def show_vars(self) -> str:
        values = self._variable_types()
        return "No variables created yet." if not values else f"Available variables: {values}"

    def submit_answer(
        self,
        answer: str,
        evidence: list[dict[str, Any]] | None = None,
        uncertainties: list[str] | None = None,
    ) -> None:
        self.answer["content"] = {
            "schema_version": "1.0",
            "answer": answer,
            "evidence": evidence or [],
            "uncertainties": uncertainties or [],
        }
        self.answer["ready"] = True

    def _variable_types(self) -> dict[str, str]:
        return {
            name: type(value).__name__
            for name, value in sorted(self.namespace.items())
            if not name.startswith("_") and name not in self.protected
        }

    def execute(self, code: str) -> dict[str, Any]:
        started = time.perf_counter()
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        try:
            tree = ast.parse(code, mode="exec")
            _CodeValidator().visit(tree)
            compiled = compile(tree, "<rcodex-repl>", "exec")
            with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
                exec(compiled, self.namespace, self.namespace)
        except BaseException as exc:
            stderr_buffer.write(f"{type(exc).__name__}: {exc}\n")
        finally:
            for name, value in self.protected.items():
                self.namespace[name] = value

        stdout, stdout_truncated = _truncate_utf8(stdout_buffer.getvalue(), self.max_output_bytes)
        stderr, stderr_truncated = _truncate_utf8(stderr_buffer.getvalue(), self.max_output_bytes)
        final_payload: Any | None = None
        if self.answer.get("ready"):
            final_payload = self.answer.get("content")
            try:
                json.dumps(final_payload, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                stderr = (stderr + "Final answer is not strict JSON data.\n")[
                    -self.max_output_bytes :
                ]
                final_payload = None
            self.answer = _AnswerDict()
            self.protected["answer"] = self.answer
            self.namespace["answer"] = self.answer
        return {
            "type": "execution_result",
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "variable_types": self._variable_types(),
            "final_payload": final_payload,
            "duration_ms": round((time.perf_counter() - started) * 1000),
        }


def main() -> int:
    try:
        initialization = _receive()
        if initialization.get("type") != "initialize":
            raise ValueError("first REPL protocol message must initialize the worker")
        raw_tools = initialization.get("tool_names", [])
        maximum = initialization.get("max_output_bytes")
        if not isinstance(raw_tools, list) or not all(isinstance(item, str) for item in raw_tools):
            raise ValueError("tool_names must be a string list")
        if not isinstance(maximum, int) or maximum <= 0:
            raise ValueError("max_output_bytes must be positive")
        worker = _Worker(raw_tools, maximum)
        _send({"type": "ready"})
        while True:
            message = _receive()
            message_type = message.get("type")
            if message_type == "close":
                _send({"type": "closed"})
                return 0
            if message_type != "execute" or not isinstance(message.get("code"), str):
                _send({"type": "protocol_error", "error": "expected execute or close"})
                continue
            _send(worker.execute(message["code"]))
    except EOFError:
        return 0
    except BaseException as exc:
        _send({"type": "worker_error", "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
