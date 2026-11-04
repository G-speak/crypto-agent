#!/usr/bin/env python3
"""Offline descriptive follow-up, not a strategy backtest or a trading signal.

Read exported decision JSONL files, including rotations. Never queries a server.
Uses the FIRST valid scan at/after each horizon, at most ten minutes late.
Missing future data stays pending/missing, not a zero return. Overlapping
candidates are not independent trades and must not be summed as portfolio PnL.
"""
import argparse
from bisect import bisect_left
from datetime import datetime
import json
import math
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clients.radar_shadow import positive, VERSION


def timestamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return dt.timestamp()


def evaluate(events, fee_bps=10, slippage_bps=10):
    for value in (fee_bps, slippage_bps):
        if not math.isfinite(value) or not 0 <= value < 10000:
            raise ValueError('invalid cost assumption')
    candidates = {}; prices = {}; latest = None
    for event in events:
        try:
            ts = timestamp(event['ts'])
            if event.get('event') == 'scan':
                for row in event['coins']:
                    try:
                        p = positive(row['price'])
                        prices.setdefault(row['coin'], {})[ts] = p
                    except (KeyError, ValueError, TypeError, OverflowError):
                        continue
                latest = ts if latest is None else max(latest, ts)
            elif (event.get('event') == 'radar_shadow_candidate'
                  and event.get('rule_version') == VERSION):
                positive(event['price'])
                # One candidate per coin+scan even if rotations/files overlap.
                candidates.setdefault((event['scan_id'], event['coin']), (ts, event))
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    timelines = {coin: sorted(rows) for coin, rows in prices.items()}
    output = []
    f, s = fee_bps / 10000, slippage_bps / 10000
    for ts, event in sorted(candidates.values(), key=lambda x: x[0]):
        row = {k: event[k] for k in ('candidate_id', 'coin', 'ts', 'price')}
        row['horizons'] = {}
        coin = event['coin']; times = timelines.get(coin, [])
        for hours in (4, 24):
            target = ts + hours * 3600
            i = bisect_left(times, target)
            if i == len(times) or times[i] > target + 600:
                result = {'status': 'pending' if latest is None or latest < target else 'missing_data'}
            else:
                observed = times[i]; exit_price = prices[coin][observed]
                ratio = exit_price / positive(event['price'])
                result = {'status': 'observed', 'delay_seconds': observed - target,
                          'observed_at': datetime.fromtimestamp(observed, datetime.fromisoformat(event['ts']).tzinfo).isoformat(),
                          'price': exit_price, 'price_change_pct': (ratio - 1) * 100,
                          'hypothetical_long_net_pct':
                              (ratio * (1-s) * (1-f) / ((1+s) * (1+f)) - 1) * 100}
            row['horizons'][str(hours)] = result
        output.append(row)
    return {'rule_version': VERSION, 'candidate_count': len(output),
            'assumptions': {'fee_bps_each_side': fee_bps, 'slippage_bps_each_side': slippage_bps,
                            'maximum_lateness_seconds': 600},
            'limitations': ['descriptive_price_followup_not_seven_role_decisions',
                           'no_position_or_cash_eligibility_assumed',
                           'overlapping_samples_not_portfolio_returns',
                           'costs_are_assumptions_not_actual_account_fees'],
            'candidates': output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--fee-bps', type=float, default=10)
    parser.add_argument('--slippage-bps', type=float, default=10)
    args = parser.parse_args()
    events = []; malformed = 0
    for path in args.files:
        with path.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict): raise ValueError('not object')
                    events.append(event)
                except (ValueError, TypeError):
                    malformed += 1
    result = evaluate(events, args.fee_bps, args.slippage_bps)
    result['malformed_lines'] = malformed
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))

if __name__ == '__main__':
    main()
