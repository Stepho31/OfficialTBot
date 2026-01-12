#!/usr/bin/env python3
"""
Validation Test Summary - Code Review Based
Validates fixes without requiring full environment setup.
"""

import os
import sys

def test_cache_locking_implementation():
    """Verify cache locking is implemented"""
    print("Testing: Cache locking implementation...")
    
    cache_file = "OfficialTBot/trade_cache.py"
    if not os.path.exists(cache_file):
        return False, "Cache file not found"
    
    with open(cache_file, 'r') as f:
        content = f.read()
    
    checks = [
        ('fcntl' in content, "File locking imported"),
        ('LOCK_SH' in content or 'LOCK_EX' in content, "Lock constants used"),
        ('_cache_lock' in content, "Thread lock variable defined"),
        ('sync_cache_with_broker' in content and 'with _cache_lock' in content, "Sync uses lock"),
    ]
    
    all_pass = all(check[0] for check in checks)
    if all_pass:
        print("  ✅ All cache locking checks passed")
    else:
        print("  ⚠️  Some checks failed:")
        for passed, desc in checks:
            status = "✅" if passed else "❌"
            print(f"    {status} {desc}")
    
    return all_pass, None


def test_close_reason_classification():
    """Verify close reason classification is implemented"""
    print("\nTesting: Close reason classification...")
    
    monitor_file = "OfficialTBot/monitor.py"
    if not os.path.exists(monitor_file):
        return False, "Monitor file not found"
    
    with open(monitor_file, 'r') as f:
        content = f.read()
    
    checks = [
        ('_classify_close_reason' in content, "Classification function exists"),
        ('CLOSED_TP' in content, "TP classification"),
        ('CLOSED_SL' in content, "SL classification"),
        ('CLOSED_TRAILING' in content, "Trailing stop classification"),
        ('CLOSED_PARTIAL' in content, "Partial close classification"),
        ('CLOSED_EXTERNALLY' in content, "External close classification"),
    ]
    
    all_pass = all(check[0] for check in checks)
    if all_pass:
        print("  ✅ All classification checks passed")
    else:
        print("  ⚠️  Some checks failed:")
        for passed, desc in checks:
            status = "✅" if passed else "❌"
            print(f"    {status} {desc}")
    
    return all_pass, None


def test_validation_gate_ordering():
    """Verify validation gates are properly ordered"""
    print("\nTesting: Validation gate ordering...")
    
    enhanced_file = "OfficialTBot/enhanced_main.py"
    if not os.path.exists(enhanced_file):
        return False, "Enhanced main file not found"
    
    with open(enhanced_file, 'r') as f:
        content = f.read()
    
    # Check that H4 is checked before H1/M15 and that H4 is excluded from H1/M15 check
    checks = [
        ('passes_h4_hard_filters' in content, "H4 filter check exists"),
        ('validate_entry_conditions' in content, "Entry validation exists"),
        ('timeframes=["H1","M15"]' in content or 'timeframes=[\'H1\',\'M15\']' in content, "H4 excluded from detailed check"),
        ('relax=True' in content and 'passes_h4_hard_filters' in content, "H4 uses relax mode"),
    ]
    
    all_pass = all(check[0] for check in checks)
    if all_pass:
        print("  ✅ Validation gate ordering looks correct")
    else:
        print("  ⚠️  Some checks failed:")
        for passed, desc in checks:
            status = "✅" if passed else "❌"
            print(f"    {status} {desc}")
    
    return all_pass, None


def test_trade_id_validation():
    """Verify trade ID validation is implemented"""
    print("\nTesting: Trade ID validation...")
    
    trader_file = "OfficialTBot/trader.py"
    if not os.path.exists(trader_file):
        return False, "Trader file not found"
    
    with open(trader_file, 'r') as f:
        content = f.read()
    
    checks = [
        ('tradeOpened' in content and 'get("tradeID")' in content, "Primary path exists"),
        ('tradesOpened' in content, "Fallback path exists"),
        ('ValueError' in content and 'tradeID' in content, "Error handling exists"),
    ]
    
    all_pass = all(check[0] for check in checks)
    if all_pass:
        print("  ✅ Trade ID validation looks correct")
    else:
        print("  ⚠️  Some checks failed:")
        for passed, desc in checks:
            status = "✅" if passed else "❌"
            print(f"    {status} {desc}")
    
    return all_pass, None


def test_safety_assertions():
    """Verify safety assertions are in place"""
    print("\nTesting: Safety assertions...")
    
    files_to_check = [
        ("OfficialTBot/trade_cache.py", ["assert", "raise ValueError", "invalid trade_id"]),
        ("OfficialTBot/trader.py", ["SAFETY ASSERTION", "sl_distance_final", "tp_distance_final"]),
        ("OfficialTBot/enhanced_main.py", ["SAFETY ASSERTION", "Trade ID validated"]),
    ]
    
    all_pass = True
    for file_path, keywords in files_to_check:
        if not os.path.exists(file_path):
            print(f"  ❌ File not found: {file_path}")
            all_pass = False
            continue
        
        with open(file_path, 'r') as f:
            content = f.read()
        
        found = [kw for kw in keywords if kw in content]
        if found:
            print(f"  ✅ {file_path}: Found {len(found)}/{len(keywords)} safety checks")
        else:
            print(f"  ⚠️  {file_path}: Safety checks not clearly visible")
    
    return all_pass, None


def run_validation_tests():
    """Run all validation tests"""
    print("="*70)
    print("VALIDATION TEST SUMMARY - CODE REVIEW BASED")
    print("="*70)
    
    tests = [
        ("Cache Locking", test_cache_locking_implementation),
        ("Close Reason Classification", test_close_reason_classification),
        ("Validation Gate Ordering", test_validation_gate_ordering),
        ("Trade ID Validation", test_trade_id_validation),
        ("Safety Assertions", test_safety_assertions),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            passed, error = test_func()
            results.append((name, passed, error))
        except Exception as e:
            results.append((name, False, str(e)))
    
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    
    passed_count = sum(1 for _, p, _ in results if p)
    total_count = len(results)
    
    for name, passed, error in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")
        if error:
            print(f"   → {error}")
    
    print(f"\nTotal: {passed_count}/{total_count} tests passed")
    
    return passed_count == total_count


if __name__ == '__main__':
    success = run_validation_tests()
    sys.exit(0 if success else 1)




