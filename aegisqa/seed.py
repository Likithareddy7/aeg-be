"""Creates the first admin login from ADMIN_USERNAME / ADMIN_PASSWORD.

Safe to run on every deploy: it does nothing once any user exists.
`--demo` also creates a demo project pointing at a public practice site, for
local exploration only -- never run it against a client database.

  python -m aegisqa.seed [--demo]
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select

from .auth.passwords import hash_password
from .config import settings
from .db import models as m
from .db.base import dispose_engine, new_id, session_scope

DEMO_PROJECT = "Demo: The Internet (login page)"
DEMO_URL = "https://the-internet.herokuapp.com/login"


async def seed_admin() -> None:
  async with session_scope() as db:
    if (await db.execute(select(func.count()).select_from(m.User))).scalar_one() > 0:
      if (await db.execute(select(func.count()).select_from(m.User).where(m.User.role == "admin"))).scalar_one() == 0:
        oldest = (await db.execute(select(m.User).order_by(m.User.created_at).limit(1))).scalar_one()
        oldest.role = "admin"
        print(f'Promoted existing user "{oldest.username}" to admin (no admin existed).')
      return
    if not settings.admin_password:
      print("No users exist yet and ADMIN_PASSWORD is not set -- set ADMIN_USERNAME/ADMIN_PASSWORD and re-run the seed.")
      return
    db.add(m.User(id=new_id(), username=settings.admin_username, password_hash=hash_password(settings.admin_password), role="admin"))
    print(f'Seeded admin user "{settings.admin_username}".')


async def seed_demo_project() -> None:
  async with session_scope() as db:
    if (await db.execute(select(m.Project.id).where(m.Project.name == DEMO_PROJECT))).first():
      print("Demo project already exists, skipping.")
      return
    project = m.Project(id=new_id(), name=DEMO_PROJECT)
    db.add(project)
    await db.flush()
    db.add(m.TargetUrl(id=new_id(), project_id=project.id, url=DEMO_URL))
    print(f'Seeded project "{DEMO_PROJECT}" ({project.id}) with URL {DEMO_URL}')


async def main(argv: list[str]) -> None:
  try:
    await seed_admin()
    if "--demo" in argv:
      await seed_demo_project()
  finally:
    await dispose_engine()


if __name__ == "__main__":
  asyncio.run(main(sys.argv[1:]))
