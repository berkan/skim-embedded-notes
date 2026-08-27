#!/usr/bin/env python3
"""Patch Skim so markup annotations live in the PDF and stay editable.

Three changes, all rewiring code paths Skim already ships:

  1. save   - write the PDFDocument (annotations embedded) instead of the
              untouched original bytes
  2. save   - stop also writing the notes to extended attributes, while still
              stripping any stale ones, so a reopened file cannot show each
              note twice
  3. open   - run the existing embedded-to-Skim-note conversion automatically,
              which is what makes them editable and puts them in the notes pane

Everything is behind the SKEmbedNotesInPDF default, so an unpatched-looking
Skim is one `defaults write` away if something misbehaves.

Idempotent: re-running detects the markers and does nothing.

    python3 apply_patch.py /path/to/skim-checkout [--revert]
"""
import sys
from pathlib import Path

KEY_DECL_H = "extern NSString *SKAutoSaveSkimNotesKey;"
KEY_DECL_M = 'NSString *SKAutoSaveSkimNotesKey = @"SKAutoSaveSkimNotes";'

# (file, anchor, replacement, marker-unique-to-the-patched-state)
EDITS = [
    # ---- 1. the preference key ------------------------------------------- #
    ("SKStringConstants.h", KEY_DECL_H,
     KEY_DECL_H + "\nextern NSString *SKEmbedNotesInPDFKey;",
     "extern NSString *SKEmbedNotesInPDFKey;"),

    ("SKStringConstants.m", KEY_DECL_M,
     KEY_DECL_M + '\nNSString *SKEmbedNotesInPDFKey = @"SKEmbedNotesInPDF";',
     'NSString *SKEmbedNotesInPDFKey ='),

    # ---- 2. save: embed rather than write the original bytes back --------- #
    ("SKMainDocument.m",
     """        if (mdFlags.exportOption == SKExportOptionWithEmbeddedNotes)
            didWrite = [[self pdfDocument] writeToURL:absoluteURL];
        else
            didWrite = [pdfData writeToURL:absoluteURL options:0 error:&error];""",
     """        // SKEmbedNotesInPDF: persist notes as PDF annotations on every save,
        // not only when exporting. [self pdfDocument] already holds them as
        // live PDFKit annotations, so this is the same code the export path
        // has always used.
        if (mdFlags.exportOption == SKExportOptionWithEmbeddedNotes ||
            (mdFlags.exportOption == SKExportOptionDefault && SKEmbedsNotesInPDF()))
            didWrite = [[self pdfDocument] writeToURL:absoluteURL];
        else
            didWrite = [pdfData writeToURL:absoluteURL options:0 error:&error];""",
     "mdFlags.exportOption == SKExportOptionDefault && SKEmbedsNotesInPDF()"),

    # ---- 3. save: strip stale EAs but do not write new ones --------------- #
    ("SKMainDocument.m",
     "    BOOL attachNotes = [self canAttachNotesForType:typeName] && mdFlags.exportOption == SKExportOptionDefault;",
     """    BOOL attachNotes = [self canAttachNotesForType:typeName] && mdFlags.exportOption == SKExportOptionDefault;
    // When the notes are going into the PDF itself we must still remove any
    // extended attributes left from before, or reopening the file would show
    // every note twice - once from the EA copy, once from the PDF.
    BOOL stripNotes = attachNotes;
    if (attachNotes && SKEmbedsNotesInPDF())
        attachNotes = NO;""",
     "BOOL stripNotes = attachNotes;"),

    # the strip block and the attach block are both gated on attachNotes;
    # re-gate the strip block on stripNotes so it still runs in embed mode
    ("SKMainDocument.m",
     "    if (attachNotes && [self fileURL] && (saveOperation == NSSaveOperation || saveOperation == NSAutosaveInPlaceOperation)) {",
     "    if (stripNotes && [self fileURL] && (saveOperation == NSSaveOperation || saveOperation == NSAutosaveInPlaceOperation)) {",
     "    if (stripNotes && [self fileURL]"),

    # ---- 4. the helper ---------------------------------------------------- #
    ("SKMainDocument.m",
     "- (BOOL)canAttachNotesForType:(NSString *)typeName {",
     """BOOL SKEmbedsNotesInPDF(void) {
    return [[NSUserDefaults standardUserDefaults] boolForKey:SKEmbedNotesInPDFKey];
}

- (BOOL)canAttachNotesForType:(NSString *)typeName {""",
     "BOOL SKEmbedsNotesInPDF(void)"),

    # ---- 5. open: convert embedded annotations to editable Skim notes ----- #
    ("SKMainDocument.m",
     """    if (wasVisible == NO)
        [[NSNotificationCenter defaultCenter] postNotificationName:SKDocumentDidShowNotification object:self];
}""",
     """    if (wasVisible == NO)
        [[NSNotificationCenter defaultCenter] postNotificationName:SKDocumentDidShowNotification object:self];

    // Embedded annotations are read-only until converted. Converting them on
    // open is what makes highlights from other devices editable and lists them
    // in the notes pane. convertNotes replaces rather than duplicates them,
    // and the save path above writes them straight back into the PDF.
    if (wasVisible == NO && SKEmbedsNotesInPDF() && mdFlags.convertingNotes == 0 &&
        [self hasConvertibleAnnotations])
        [self convertNotes];
}""",
     "[self hasConvertibleAnnotations])"),
]


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    root = Path(sys.argv[1])
    revert = "--revert" in sys.argv
    if not (root / "SKMainDocument.m").exists():
        sys.exit(f"not a Skim checkout: {root}")

    changed = skipped = 0
    for rel, old, new, marker in EDITS:
        p = root / rel
        s = p.read_text(encoding="utf-8")
        frm, to = (new, old) if revert else (old, new)
        applied = marker in s
        if applied != revert:
            print(f"  skip  {rel}: already {'reverted' if revert else 'applied'}")
            skipped += 1
            continue
        if frm not in s:
            print(f"  FAIL  {rel}: anchor not found:\n        {frm.splitlines()[0][:78]}")
            return 1
        if s.count(frm) != 1:
            print(f"  FAIL  {rel}: anchor matches {s.count(frm)} times, expected 1")
            return 1
        p.write_text(s.replace(frm, to, 1), encoding="utf-8")
        print(f"  ok    {rel}: {'reverted' if revert else 'patched'}")
        changed += 1

    print(f"\n{changed} edit(s) applied, {skipped} already in place")
    if not revert:
        print("\nEnable at runtime with:")
        print("  defaults write -app Skim SKEmbedNotesInPDF -bool YES")
        print("Disable (behaves like stock Skim again):")
        print("  defaults write -app Skim SKEmbedNotesInPDF -bool NO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
