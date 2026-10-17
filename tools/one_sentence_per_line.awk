# Keeps the prose of a Markdown file to one sentence per line. Run as `awk -v mode=check -f <this file> <files>`, which
# reports each line that holds two sentences, or a sentence that carries on to the next line, and exits 1 if there are
# any; `mode=reflow` prints the file with each paragraph's sentences split onto their own lines instead.
#
# What is prose: a paragraph, or a list item and the lines that continue it. Code fences, tables, headings, HTML,
# blockquotes, link definitions and YAML frontmatter are left alone. A sentence ends at `.`, `!` or `?` followed by a
# space and a capital, a backtick, a bracket or an asterisk, outside code spans and link text. `e.g.`, `i.e.` and `vs.`
# do not end one.

function spaces(n,   s) {
  s = ""
  while (n-- > 0) s = s " "
  return s
}

function rtrim(s) {
  sub(/[ \t]+$/, "", s)
  return s
}

function is_struct(l) {
  return l ~ /^[ \t]*$/ || l ~ /^[ \t]*#/ || l ~ /^[ \t]*\|/ || l ~ /^[ \t]*</ || l ~ /^[ \t]*>/ \
    || l ~ /^[ \t]*-{3,}[ \t]*$/ || l ~ /^[ \t]*\[[^]]*\]:/
}

function is_item(l) {
  return l ~ /^[ \t]*([-*+][ \t]+|[0-9]+[.)][ \t]+)/
}

# The indentation and list marker that open a line, so a sentence can be put back under them.
function prefix_of(l,   p) {
  p = l
  match(p, /^[ \t]*([-*+][ \t]+|[0-9]+[.)][ \t]+)?/)
  return substr(p, 1, RLENGTH)
}

# The prose of a line, without its prefix.
function text_of(l, first,   t) {
  if (first) return substr(l, length(prefix_of(l)) + 1)
  t = l
  sub(/^[ \t]+/, "", t)
  return t
}

# Splits `t` into its sentences, in `out`, and returns how many there are.
function split_sentences(t, out,   n, i, c, start, depth, incode, k, nc, before, len) {
  delete out
  n = 0
  start = 1
  depth = 0
  incode = 0
  len = length(t)
  for (i = 1; i <= len; i++) {
    c = substr(t, i, 1)
    if (c == "`") {
      incode = !incode
      continue
    }
    if (incode) continue
    if (c == "[") {
      depth++
      continue
    }
    if (c == "]") {
      if (depth > 0) depth--
      continue
    }
    if (depth > 0 || (c != "." && c != "!" && c != "?")) continue
    k = i + 1
    if (substr(t, k, 1) != " ") continue
    while (substr(t, k, 1) == " ") k++
    nc = substr(t, k, 1)
    if (nc !~ /^[A-Z`*(]$/ && nc != "[") continue
    before = substr(t, 1, i)
    if (before ~ /([eE]\.g|[iI]\.e|[vV]s)\.$/) continue
    out[++n] = substr(t, start, i - start + 1)
    start = k
  }
  if (start <= len) out[++n] = substr(t, start)
  return n
}

function emit(l) {
  if (mode == "reflow") print l
}

function report(n, why) {
  printf "%s:%d: %s\n", FILENAME, n, why > "/dev/stderr"
  bad = 1
}

# Ends the paragraph collected in B: checks it (check mode) or prints it split into sentences (reflow mode).
function flush(   i, text, n, k, pref, cont, t) {
  if (nb == 0) return
  if (mode == "check") {
    for (i = 1; i <= nb; i++) {
      t = text_of(B[i], i == 1)
      if (split_sentences(t, S) > 1) report(BN[i], "two sentences on one line")
      if (i < nb && B[i] !~ /[.!?:][ \t]*$/) report(BN[i], "a sentence carries on to the next line")
    }
  } else {
    pref = prefix_of(B[1])
    text = substr(B[1], length(pref) + 1)
    for (i = 2; i <= nb; i++) text = text " " text_of(B[i], 0)
    n = split_sentences(rtrim(text), S)
    cont = spaces(length(pref))
    for (k = 1; k <= n; k++) print (k == 1 ? pref : cont) rtrim(S[k])
  }
  nb = 0
}

function add(l) {
  B[++nb] = l
  BN[nb] = FNR
}

BEGIN {
  if (mode != "check" && mode != "reflow") {
    print "mode must be check or reflow" > "/dev/stderr"
    exit 2
  }
}

FNR == 1 {
  flush()
  infence = 0
  infm = ($0 == "---")
}

{
  if (infm) {
    if (FNR > 1 && $0 == "---") infm = 0
    emit($0)
    next
  }
  if ($0 ~ /^[ \t]*```/) {
    flush()
    infence = !infence
    emit($0)
    next
  }
  if (infence) {
    emit($0)
    next
  }
  if (is_struct($0)) {
    flush()
    emit($0)
    next
  }
  if (is_item($0)) flush()
  add($0)
}

END {
  flush()
  if (bad) exit 1
}
