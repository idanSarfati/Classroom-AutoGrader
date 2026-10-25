import os

env_path = os.path.join(
    r"c:\Users\Idan Sarfati\Desktop\projects\Clasroom AutoGrader", ".env"
)
for raw in open(env_path, encoding="utf-8"):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        print(line)
        continue
    key, _, val = line.partition("=")
    val = val.strip()
    if any(s in key.upper() for s in ("KEY", "TOKEN", "SECRET")):
        if not val:
            shown = "(empty)"
        elif "," in val or ";" in val:
            shown = "(set: multiple values)"
        else:
            shown = f"(set: {val[:4]}...{val[-3:]}, len={len(val)})"
        print(f"{key} = {shown}")
    else:
        print(f"{key} = {val}")
