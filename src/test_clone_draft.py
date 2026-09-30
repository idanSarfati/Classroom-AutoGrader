"""Test cloning an existing draft coursework to create a Developer-Managed draft."""

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from src.bidi_utils import bidi_print


def main():
    # Load credentials directly from existing token
    creds = Credentials.from_authorized_user_file("token.json")
    service = build("classroom", "v1", credentials=creds)

    # 1. Fetch active courses
    courses_res = service.courses().list(courseStates=["ACTIVE"]).execute()
    courses = courses_res.get("courses", [])

    print("\n--- Active Courses ---")
    for idx, c in enumerate(courses, 1):
        print(f"[{idx}] {bidi_print(c.get('name', ''))}")

    choice = input("\nSelect course number: ").strip()
    course = courses[int(choice) - 1]
    course_id = course["id"]

    # 2. Fetch DRAFT coursework for this course
    cw_res = (
        service.courses()
        .courseWork()
        .list(courseId=course_id, courseWorkStates=["DRAFT"])
        .execute()
    )
    drafts = cw_res.get("courseWork", [])

    if not drafts:
        print("No DRAFT coursework found in this course.")
        return

    print(f"\n--- Draft Coursework in {bidi_print(course['name'])} ---")
    for idx, d in enumerate(drafts, 1):
        print(f"[{idx}] {bidi_print(d.get('title', ''))} (ID: {d['id']})")

    d_choice = input("\nSelect draft to clone: ").strip()
    source_draft = drafts[int(d_choice) - 1]

    # 3. Prepare payload for the managed draft
    new_title = f"{source_draft.get('title', '')} [Managed Test]"
    body = {
        "title": new_title,
        "description": source_draft.get("description", ""),
        "workType": source_draft.get("workType", "ASSIGNMENT"),
        "state": "DRAFT",
    }

    # Preserve Topic (the tab/section in Classroom)
    if "topicId" in source_draft:
        body["topicId"] = source_draft["topicId"]

    # Preserve Points, Due dates, and Attached Materials (Docs, Sheets, links, etc.)
    if "maxPoints" in source_draft:
        body["maxPoints"] = source_draft["maxPoints"]
    if "dueDate" in source_draft:
        body["dueDate"] = source_draft["dueDate"]
    if "dueTime" in source_draft:
        body["dueTime"] = source_draft["dueTime"]
    if "materials" in source_draft:
        body["materials"] = source_draft["materials"]

    print(f"\nCloning as managed draft into original topic: {bidi_print(new_title)}...")

    # 4. Create the new coursework
    try:
        created = (
            service.courses()
            .courseWork()
            .create(courseId=course_id, body=body)
            .execute()
        )
        print("\nSuccess! Managed draft created successfully.")
        print(f"New CourseWork ID: {created['id']}")
        print(f"Title: {bidi_print(created['title'])}")
        print(f"Topic ID: {created.get('topicId', 'None')}")
        print("Check your Google Classroom UI — you should see the new draft right inside its topic.")
    except HttpError as err:
        print(f"\nFailed to create draft (HTTP {err.resp.status}): {err}")


if __name__ == "__main__":
    main()