"""Stand-in for a model CLI: reads the prompt on stdin, answers with a random
valid option as JSON, and sometimes replies with junk to exercise retries."""
import json
import random
import re
import sys

prompt = sys.stdin.read()
n = len(re.findall(r"^\d+\. ", prompt.split("== DECISION ==")[-1], re.M))
if random.random() < 0.05:
    print("I think I'll go with the second option!")
else:
    print(json.dumps({"reasoning": "mock", "choice": random.randrange(n)}))
