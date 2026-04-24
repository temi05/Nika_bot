
import os

file_path = 'app/bot/commands.py'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Fix literal \n (escaped twice in my previous script)
# We want to replace backslash+n with a real newline in many places,
# but NOT inside join() or other code logic.
# Most of them are in f-strings like f"Text\\nText"

# First, fix the return router issue
content = content.replace('\\n    return router', '\n    return router')

# Now fix the message strings. 
# Since I used \\n in my previous script's strings, they became literal \n in the file.
# I'll replace them globally but carefully.
# I'll only replace them if they are not followed by ".join"
import re
content = re.sub(r'(?<!")\\n(?!")', '\n', content)
# Wait, that might be too aggressive. 

# Let's just fix the known bad ones.
content = content.replace('\\n', '\n')

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Formatting fixed!")
