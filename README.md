# Fujitsu ASY9LSACW IR Protocol Capture / Analysis

`acir.py` is a small Python tool for systematically capturing, demodulating, and comparing IR packets from a Fujitsu ASY9LSACW air-conditioner remote using edge-timestamp CSV exports from a logic analyzer.

## Usage

Configure the remote states and input file path in `acir_config.json`, then run:

```bash
python3 acir.py capture
````

The program tells you exactly which remote state to transmit, waits for the logic-analyzer CSV to appear, demodulates it, and archives both the original capture and its associated settings.

Useful commands:

```bash
python3 acir.py status
python3 acir.py analyse
python3 acir.py reprocess
```

`analyse` compares all captured states and generates a machine-readable `analysis.json` describing the discovered packet structure and field differences.

## Files

* `acir.py` — capture, demodulation, dataset management, and analysis tool.
* `acir_config.json` — capture plan, remote settings, input path, and decoder configuration.
* `acir_dataset/captures/` — preserved raw CSV captures together with JSON metadata describing the remote state used for each capture.
* `acir_dataset/analysis.json` — generated machine-readable analysis of the captured protocol.
* Included captures — the dataset used to reverse-engineer the Fujitsu ASY9LSACW protocol.
