"""Handwritten oracle fixture for verifier tests; never seeded into agent workspaces."""
import os
import sqlite3
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

app = FastAPI()
db = sqlite3.connect(os.environ["DATABASE_PATH"], check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS tasks(seq INTEGER PRIMARY KEY,id TEXT UNIQUE,title TEXT,status TEXT)")
db.commit()


class Create(BaseModel):
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class Update(BaseModel):
    status: Literal["todo", "doing", "done"]


def item(row):
    return dict(zip(("id", "title", "status"), row))


@app.post("/tasks", status_code=201)
def create(value: Create):
    identity = uuid4().hex
    db.execute("INSERT INTO tasks(id,title,status) VALUES (?,?,?)", (identity, value.title, "todo"))
    db.commit()
    return {"id": identity, "title": value.title, "status": "todo"}


@app.get("/tasks")
def listing(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    rows = db.execute("SELECT id,title,status FROM tasks ORDER BY seq LIMIT ? OFFSET ?", (limit, offset))
    return {"items": [item(row) for row in rows], "total": db.execute("SELECT count(*) FROM tasks").fetchone()[0]}


@app.get("/tasks/{task_id}")
def read(task_id: str):
    row = db.execute("SELECT id,title,status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(404)
    return item(row)


@app.patch("/tasks/{task_id}")
def update(task_id: str, value: Update):
    existing = read(task_id)
    levels = {"todo": 0, "doing": 1, "done": 2}
    if levels[value.status] - levels[existing["status"]] not in (0, 1):
        raise HTTPException(409)
    db.execute("UPDATE tasks SET status=? WHERE id=?", (value.status, task_id))
    db.commit()
    return read(task_id)
