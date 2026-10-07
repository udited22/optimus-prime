# configs/backtest

`synthetic_spreads.toml`: the B-02 synthetic bid-ask spread model. **Status: ASSUMED.** None of it is measured.
We have no NIFTY option quote history, so the numbers are pessimistic placeholders that we chose ourselves. They
stay ASSUMED until the D-10 tick recorder measures real spreads and a calibration replaces this file. The loader
(`backtest/spreads.py`) refuses any other status. Every run that uses the model writes its version and the ASSUMED
label into the ledger's START entry, and each FILL entry records the half-spread that the bar had to clear.
