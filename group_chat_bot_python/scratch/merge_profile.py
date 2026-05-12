import os

RAW = "scratch/profile_raw.py"
DEST = "app/bot/routers/profile_ai.py"

def main():
    with open(RAW, "r", encoding="utf-8") as f:
        raw_lines = f.read()

    with open(DEST, "r", encoding="utf-8") as f:
        dest_content = f.read()
        
    final_dest = dest_content.replace("    # We will insert the raw lines here", raw_lines)
    with open(DEST, "w", encoding="utf-8") as f:
        f.write(final_dest)

    print("Profile merge complete.")

if __name__ == "__main__":
    main()
