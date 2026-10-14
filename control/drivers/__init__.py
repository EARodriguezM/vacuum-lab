"""Instrument drivers: one package per controller, each speaking its own
protocol over pyserial and knowing nothing of the panel.

    tc600/   Pfeiffer TC 600 drive unit -- RS-485 telegrams, parameter table
    tpg/     Pfeiffer TPG 252 A / 26x gauge controllers -- RS-232 mnemonics

The panel's services (lab/pump.py, lab/gauge.py) wrap them; a chamber's file
in chambers/ says which one fills which role.
"""
