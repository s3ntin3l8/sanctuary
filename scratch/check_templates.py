import os

templates_dir = "app/templates"
search_dir = "app"

template_files = []
for root, dirs, files in os.walk(templates_dir):
    for file in files:
        if file.endswith(".html"):
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, templates_dir)
            template_files.append(rel_path)

orphaned = []
for template in template_files:
    # Escape for regex
    pattern = template.replace(".", "\\.")
    # Search in all files in app/
    found = False
    for root, dirs, files in os.walk(search_dir):
        for file in files:
            # Skip the template itself when searching in templates
            if os.path.join(root, file) == os.path.join(templates_dir, template):
                continue

            # Check file content
            try:
                with open(os.path.join(root, file)) as f:
                    content = f.read()
                    if template in content:
                        found = True
                        break
            except Exception:
                pass
        if found:
            break

    if not found:
        # Special case: base.html is often extended without "templates/" prefix but it's the root.
        # But our script checks for rel_path which is "base.html" for base.html.
        orphaned.append(template)

print("Potential orphaned templates:")
for o in orphaned:
    print(o)
