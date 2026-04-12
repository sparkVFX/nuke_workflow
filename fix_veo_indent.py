"""
Fix veo.py: 
1. difflib alignment with good version for base indentation
2. Comprehensive try/except/finally/if/else block body indent fixing
3. Handle all new code sections that don't exist in good version
"""
import re, subprocess, difflib, py_compile

INPUT = r'e:\BaiduNetdiskDownload\nuke_workflow\ai_workflow\veo.py'

with open(INPUT, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Step 1: Remove global 4-space indent from line 46+
clean = list(lines[:45])
for i in range(45, len(lines)):
    line = lines[i]
    if line.strip() == '':
        clean.append(line)
    elif line.startswith('    '):
        clean.append(line[4:])
    else:
        clean.append(line)

# Get the good version from git
proc = subprocess.run(
    ['git', 'show', '76bb8ce:ai_workflow/veo.py'],
    capture_output=True, cwd=r'e:\BaiduNetdiskDownload\nuke_workflow'
)
good_content = proc.stdout.decode('utf-8', errors='replace').replace('\x00', '')
good_lines = good_content.split('\n')

# Step 2: difflib alignment
good_stripped_content = [l.strip() for l in good_lines]
clean_stripped_content = [l.strip() for l in [l.rstrip() for l in clean]]
good_rstripped = [l.rstrip() for l in good_lines]
clean_rstripped = [l.rstrip() for l in clean]

sm = difflib.SequenceMatcher(None, good_stripped_content, clean_stripped_content, autojunk=False)

final_lines = []

def get_indent(line):
    if not line or not line.strip():
        return 0
    return len(line) - len(line.lstrip())

def is_block_opener(line):
    s = line.rstrip()
    if not s or s.lstrip().startswith('#'):
        return False
    if not s.endswith(':'):
        return False
    ls = s.lstrip()
    return bool(re.match(r'^(def |class |if |elif |else:|for |while |try:|except |except:|finally:|with |async )', ls))

for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag == 'equal':
        for k in range(i2 - i1):
            final_lines.append(good_rstripped[i1 + k])
    elif tag == 'replace':
        good_chunk = good_rstripped[i1:i2]
        clean_chunk = clean_rstripped[j1:j2]
        for k in range(len(clean_chunk)):
            cl = clean_chunk[k]
            if not cl.strip():
                final_lines.append('')
                continue
            if k < len(good_chunk) and good_chunk[k].strip():
                target_indent = get_indent(good_chunk[k])
            elif final_lines and final_lines[-1].strip():
                target_indent = get_indent(final_lines[-1])
            else:
                target_indent = 0
            clean_indent = get_indent(cl)
            if k > 0 and clean_chunk[k-1].strip():
                prev_clean_indent = get_indent(clean_chunk[k-1])
                indent_delta = clean_indent - prev_clean_indent
                if k - 1 < len(good_chunk) and good_chunk[k-1].strip():
                    prev_good_indent = get_indent(good_chunk[k-1])
                    target_indent = prev_good_indent + indent_delta
            target_indent = max(0, target_indent)
            final_lines.append(' ' * target_indent + cl.lstrip())
    elif tag == 'insert':
        ref_indent = 0
        if final_lines:
            for fl in reversed(final_lines):
                if fl.strip():
                    ref_indent = get_indent(fl)
                    break
        first_clean_indent = None
        for k in range(j2 - j1):
            cl = clean_rstripped[j1 + k]
            if cl.strip():
                first_clean_indent = get_indent(cl)
                break
        if first_clean_indent is None:
            first_clean_indent = 0
        last_is_opener = False
        if final_lines:
            for fl in reversed(final_lines):
                if fl.strip():
                    if is_block_opener(fl):
                        last_is_opener = True
                    break
        if last_is_opener:
            base_indent = ref_indent + 4
        else:
            base_indent = ref_indent
        indent_offset = base_indent - first_clean_indent
        for k in range(j2 - j1):
            cl = clean_rstripped[j1 + k]
            if not cl.strip():
                final_lines.append('')
                continue
            ci = get_indent(cl)
            new_indent = max(0, ci + indent_offset)
            final_lines.append(' ' * new_indent + cl.lstrip())
    elif tag == 'delete':
        pass

# Write intermediate result
output = '\n'.join(final_lines)
if not output.endswith('\n'):
    output += '\n'
with open(INPUT, 'w', encoding='utf-8') as f:
    f.write(output)

print(f"Step 2 complete: {len(final_lines)} lines")

# Step 3: Smart iterative fixing
# Enhanced: for 'expected except/finally', find the matching try: and indent 
# ALL lines between try: and the error line, PLUS continue indenting until 
# we find an except/finally at the try: level

CONTINUATION_KW = ('else:', 'elif ', 'except:', 'except ', 'finally:')
MAX_ITER = 300
prev_errors = []

for iteration in range(MAX_ITER):
    try:
        py_compile.compile(INPUT, doraise=True)
        print(f"\nSUCCESS after {iteration} fixes!")
        break
    except py_compile.PyCompileError as e:
        err_str = str(e)
        
        # Detect infinite loop
        prev_errors.append(err_str)
        if len(prev_errors) > 5:
            prev_errors.pop(0)
        if len(prev_errors) == 5 and len(set(prev_errors)) <= 2:
            print(f"\nStuck in loop, manual intervention needed.")
            print(f"Error: {err_str}")
            # Show context
            with open(INPUT, 'r', encoding='utf-8') as f:
                fl = f.readlines()
            m_line = re.search(r'line (\d+)\)', err_str)
            if m_line:
                ln = int(m_line.group(1))
                for idx in range(max(0,ln-5), min(len(fl), ln+5)):
                    mark = ">>>" if idx+1 == ln else "   "
                    print(f"{mark} {idx+1}: {fl[idx].rstrip()}")
            break
        
        with open(INPUT, 'r', encoding='utf-8') as f:
            file_lines = f.readlines()
        
        m = re.search(r'line (\d+)\)', err_str)
        if not m:
            m = re.search(r'line (\d+)', err_str)
        if not m:
            print(f"Cannot parse: {err_str}")
            break
        error_line = int(m.group(1))
        
        if 'expected an indented block' in err_str:
            m2 = re.search(r"on line (\d+)", err_str)
            if m2:
                opener_line = int(m2.group(1))
                opener_indent = get_indent(file_lines[opener_line - 1])
                error_idx = error_line - 1
                current_indent = get_indent(file_lines[error_idx])
                
                # Indent this line and all following lines at same indent
                # until we hit a continuation keyword at opener indent
                # or a line at < opener indent
                k = error_idx
                while k < len(file_lines):
                    line = file_lines[k]
                    if not line.strip():
                        k += 1
                        continue
                    li = get_indent(line)
                    ls = line.lstrip()
                    
                    # Stop if we're back at opener level with a continuation
                    if k > error_idx and li <= opener_indent:
                        if li < opener_indent:
                            break
                        # li == opener_indent
                        if any(ls.startswith(kw) for kw in CONTINUATION_KW):
                            break
                        # Check if this is a new def/class at the same level
                        if re.match(r'^(def |class |@)', ls):
                            break
                    
                    # Only indent lines at current_indent (which equals opener_indent)
                    if li >= current_indent:
                        file_lines[k] = '    ' + file_lines[k]
                    k += 1
                
                with open(INPUT, 'w', encoding='utf-8') as f:
                    f.writelines(file_lines)
                if iteration < 30:
                    print(f"Fix #{iteration+1}: indent block L{error_line}-L{k} (opener L{opener_line})")
        
        elif 'unexpected indent' in err_str:
            error_idx = error_line - 1
            line = file_lines[error_idx]
            if line.startswith('    '):
                file_lines[error_idx] = line[4:]
                with open(INPUT, 'w', encoding='utf-8') as f:
                    f.writelines(file_lines)
                if iteration < 30:
                    print(f"Fix #{iteration+1}: dedent L{error_line}")
        
        elif 'unindent does not match' in err_str:
            error_idx = error_line - 1
            line = file_lines[error_idx]
            ci = get_indent(line)
            valid_indents = set()
            for p in range(error_idx - 1, max(0, error_idx - 50), -1):
                pl = file_lines[p]
                if pl.strip():
                    valid_indents.add(get_indent(pl))
            smaller = sorted([v for v in valid_indents if v < ci])
            if smaller:
                target = smaller[-1]
                file_lines[error_idx] = ' ' * target + line.lstrip()
                with open(INPUT, 'w', encoding='utf-8') as f:
                    f.writelines(file_lines)
                if iteration < 30:
                    print(f"Fix #{iteration+1}: align L{error_line} to {target}")
            else:
                file_lines[error_idx] = line.lstrip()
                with open(INPUT, 'w', encoding='utf-8') as f:
                    f.writelines(file_lines)
                if iteration < 30:
                    print(f"Fix #{iteration+1}: reset L{error_line}")
        
        elif "expected 'except' or 'finally'" in err_str:
            error_idx = error_line - 1
            # Find try:
            try_indent = None
            try_idx = None
            for p in range(error_idx - 1, max(0, error_idx - 300), -1):
                ls = file_lines[p].lstrip()
                if ls.startswith('try:'):
                    try_indent = get_indent(file_lines[p])
                    try_idx = p
                    break
            
            if try_idx is not None:
                # The error line is at or below try_indent, meaning it's NOT inside try body
                # We need to indent everything from try_idx+1 to error_idx-1 by +4
                # But also everything AFTER error_idx that should be in the try body
                # until we find except/finally at try_indent level
                
                # Find the matching except/finally
                end_idx = None
                for p in range(error_idx, len(file_lines)):
                    ls = file_lines[p].lstrip()
                    li = get_indent(file_lines[p])
                    if li == try_indent and (ls.startswith('except') or ls.startswith('finally:')):
                        end_idx = p
                        break
                
                if end_idx is None:
                    # No matching except/finally found at try_indent level
                    # Search for it at try_indent + 4 (it might have been indented)
                    for p in range(error_idx, len(file_lines)):
                        ls = file_lines[p].lstrip()
                        li = get_indent(file_lines[p])
                        if li <= try_indent and (ls.startswith('except') or ls.startswith('finally:')):
                            end_idx = p
                            break
                
                if end_idx is not None:
                    # Indent all lines from try_idx+1 to end_idx-1 that are at try_indent level
                    for k in range(try_idx + 1, end_idx):
                        line = file_lines[k]
                        if not line.strip():
                            continue
                        li = get_indent(line)
                        if li >= try_indent:
                            file_lines[k] = '    ' + file_lines[k]
                    
                    with open(INPUT, 'w', encoding='utf-8') as f:
                        f.writelines(file_lines)
                    if iteration < 30:
                        print(f"Fix #{iteration+1}: indent try-body L{try_idx+2}-L{end_idx} (try at L{try_idx+1}, finally/except at L{end_idx+1})")
                else:
                    print(f"Cannot find except/finally for try at L{try_idx+1}")
                    break
            else:
                print(f"Cannot find try for error at L{error_line}")
                break
        
        elif 'invalid syntax' in err_str:
            error_idx = error_line - 1
            line_content = file_lines[error_idx].lstrip()
            if any(line_content.startswith(kw) for kw in CONTINUATION_KW):
                cl = file_lines[error_idx]
                if cl.startswith('    '):
                    file_lines[error_idx] = cl[4:]
                    with open(INPUT, 'w', encoding='utf-8') as f:
                        f.writelines(file_lines)
                    if iteration < 30:
                        print(f"Fix #{iteration+1}: dedent continuation L{error_line}")
                else:
                    print(f"Cannot fix invalid syntax L{error_line}")
                    break
            else:
                print(f"Unhandled invalid syntax L{error_line}: {line_content[:60]}")
                break
        else:
            print(f"Unhandled: {err_str}")
            break
else:
    print(f"Max iterations reached ({MAX_ITER})")
    try:
        py_compile.compile(INPUT, doraise=True)
    except py_compile.PyCompileError as e:
        print(f"Remaining: {e}")
