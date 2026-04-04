import requests
import os

files = [
    ("Compliance_Affidavit.html", "<h1>Compliance Affidavit</h1><p>We attach the proof of delivery for the Motion to Dismiss (Doc 2). Signature collected by process server.</p>", "2") # Assuming Doc 2 is the Motion to dismiss
]

for name, content, parent_id in files:
    file_path = f"/tmp/{name}"
    with open(file_path, "w") as f:
        f.write(content)
        
    with open(file_path, "rb") as f:
        print(f"Uploading {name} as a child to parent ID {parent_id}...")
        response = requests.post(
            "http://127.0.0.1:8000/upload",
            files={"file": (name, f, "text/html")},
            data={"case_id": "VANE-VS-VANE", "parent_id": parent_id}
        )
        print(response.json())
