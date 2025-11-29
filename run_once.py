# Entrypoint script for PythonAnywhere

from main import main

if __name__ == "__main__":
    print("[RUN_ONCE] Triggering trading bot run...")
    main()
    print("[RUN_ONCE] Run complete.")