# Google BBRv3 on the frozen android13-5.15 KMI

Origin: [Google's official BBRv3 source](https://github.com/google/bbr/blob/90210de4b779d40496dee0b89081780eeddf2a60/net/ipv4/tcp_bbr.c).
The existing 5.15 backport retains the Google model/parameters, adapts callback
signatures and random/PLB APIs, removes newer BPF kfunc registration, and
registers v3 as `bbr3` beside stock BBRv1 `bbr`.

Do not copy Google's newer-kernel TCP structures over the frozen Android KMI.
The backport stores v3 state outside the 104-byte `icsk_ca_priv` area and uses
Android genksyms guards for shared structures. The build verifies all frozen
export CRCs against the matching Google-certified release, rather than
assuming these guards suffice.

`0002-bbrv1-allocation-fallback.patch` fixes the original backport's unchecked
allocation-failure path. If `kzalloc(GFP_ATOMIC)` fails, initialization switches
the socket's operations to built-in BBRv1 and initializes inline BBRv1 state.
Both algorithms must be built in; the Kconfig dependency enforces this.
Successful allocations are freed on release. No congestion callback is left
pointing at missing v3 state.

`0003-plb-pernet-initialization.patch` registers PLB before TCP and initializes
its parameters in PLB's own namespace callback, after pernet core allocates
the context. The original backport read an unallocated generic namespace
slot from TCP's earlier callback. PLB registration now precedes TCP for the
initial namespace and all later namespaces; lookups before registration
return NULL.
