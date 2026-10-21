"""Private driver for native asynchronous validation sessions."""

from __future__ import annotations

from typing import Any

from ._native import AsyncSession


async def drive(session: AsyncSession) -> Any:
    """Await native requests until the session completes, then close it."""
    try:
        done, payload = session.start()
        while not done:
            token, awaitable = payload
            try:
                value = await awaitable
            except BaseException as error:
                done, payload = session.resume_error(token, error)
            else:
                done, payload = session.resume_value(token, value)
        return payload
    finally:
        session.close()
