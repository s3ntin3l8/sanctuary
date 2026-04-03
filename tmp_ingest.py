import requests
import os

files = [
    ("Motion_to_Dismiss.html", "<h1>Motion to Dismiss</h1><p>This is a formally submitted motion to dismiss the lawsuit under Rule 12(b)(6). The plaintiff failed to state a claim.</p>"),
    ("Settlement_Agreement.html", "<h1>Settlement Agreement</h1><p>The parties have agreed to a settlement sum of $500,000 to be paid within 30 days. This resolves all claims.</p>"),
    ("Witness_Testimony.html", "<h1>Witness Testimony</h1><p>The witness, Arthur Smith, stated under oath that they saw the defendant at the scene of the crime at 9:00 PM on the night in question.</p>")
]

for name, content in files:
    file_path = f"/tmp/{name}"
    with open(file_path, "w") as f:
        f.write(content)
        
    with open(file_path, "rb") as f:
        print(f"Uploading {name}...")
        response = requests.post(
            "http://127.0.0.1:8000/upload",
            files={"file": (name, f, "text/html")},
            data={"case_id": "VANE-VS-VANE"}
        )
        print(response.json())
