"""EEZtest — an autonomous test framework for EEZ sync-rollup chains.

An EEZ instance is an (L1, L2, composer-front) triple.  This package launches a
set of independent *workers* that each exercise one facet of the chain (funding,
fuzzing, contract calls, congestion races, DDoS, proxy-creation routing), shares
their live state on a dashboard, and writes a report after a fixed run duration.
"""

__version__ = "0.1.0"
