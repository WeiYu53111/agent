import argparse
from openai import AsyncOpenAI


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["probe", "compare", "run"])
    parser.add_argument("--real",action="store_true")
\