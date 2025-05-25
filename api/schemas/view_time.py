from typing import List

from pydantic import BaseModel


class ViewTimeLecture(BaseModel):
    lecture_name: str
    time: int


class ViewTimeSection(BaseModel):
    section_name: str
    total_time: int
    lectures: List[ViewTimeLecture]


class ViewTimeSubSkill(BaseModel):
    sub_skill_id: str
    sub_skill_name: str
    total_time: int
    sections: List[ViewTimeSection]


class ViewTime(BaseModel):
    total_time: int
    sub_skills: List[ViewTimeSubSkill]


class TotalTime(BaseModel):
    total_time: int
