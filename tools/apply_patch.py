#!/usr/bin/env python3
"""Patch Skim so embedded PDF annotations are editable, without letting PDFKit
rewrite the file.

Skim keeps notes in extended attributes, so they never sync, and annotations
embedded by another app are visible but read-only. The obvious fix - make Skim
save the PDFDocument with annotations embedded - is destructive: PDFKit
re-serialises everything, which on a scanned book decoded its JBIG2 page images
to Flate (2.8MB -> 8.1MB) and rewrote the OCR text layer into something with no
recoverable word positions (21,175 words -> 0). Measured, not theorised.

So Skim keeps its own save path untouched - it writes the original bytes and
attaches notes as extended attributes, exactly as it always has - and an
external tool grafts those notes into the PDF as real annotations using an
incremental append, which leaves the original bytes byte-for-byte intact.

What this patch therefore does:

  read side   embedded annotations become editable Skim notes on open, listed
              in the notes pane, without dirtying the document or blocking
  write side  UNCHANGED. Skim writes what it always wrote.
  handoff     on closing a document, invoke the graft tool for that file

Deliberately NOT included any more:

  * making save write [self pdfDocument] - that is the destructive rewrite
  * suppressing the extended-attribute write - those notes are now the input
    the graft tool reads, so Skim must keep producing them

Crucially the conversion must not recompute pdfData either. Stripping the
annotations out of it requires the same re-serialisation, and pdfData is what
Skim writes on save - poisoning it would put the damage back on the save path
by another route. It stays as loaded, and the notes-free form is produced
lazily only if an export or PDF bundle actually asks for one.

    python3 apply_patch.py /path/to/skim-checkout [--revert]
"""
from __future__ import annotations

import sys
from pathlib import Path

KEY_H = "extern NSString *SKAutoSaveSkimNotesKey;"
KEY_M = 'NSString *SKAutoSaveSkimNotesKey = @"SKAutoSaveSkimNotes";'

# (file, anchor, replacement, marker unique to the patched state)
EDITS = [
    # ---- preferences ------------------------------------------------------ #
    ("SKStringConstants.h", KEY_H,
     KEY_H + "\nextern NSString *SKEmbedNotesInPDFKey;"
             "\nextern NSString *SKNoteGraftToolKey;",
     "extern NSString *SKNoteGraftToolKey;"),

    ("SKStringConstants.m", KEY_M,
     KEY_M + '\nNSString *SKEmbedNotesInPDFKey = @"SKEmbedNotesInPDF";'
             '\nNSString *SKNoteGraftToolKey = @"SKNoteGraftTool";',
     'NSString *SKNoteGraftToolKey ='),

    ("SKMainDocument.h",
     "        unsigned int needsPasswordToConvert:1;",
     "        unsigned int needsPasswordToConvert:1;\n"
     "        unsigned int autoConvertingNotes:1;\n"
     "        unsigned int pdfDataNeedsStripping:1;",
     "unsigned int pdfDataNeedsStripping:1;"),

    # ---- helpers ---------------------------------------------------------- #
    ("SKMainDocument.m",
     "- (BOOL)canAttachNotesForType:(NSString *)typeName {",
     """BOOL SKEmbedsNotesInPDF(void) {
    return [[NSUserDefaults standardUserDefaults] boolForKey:SKEmbedNotesInPDFKey];
}

// pdfData is meant to be the document *without* Skim notes. Producing that
// form costs a full re-serialisation, which is exactly what we are avoiding,
// so the automatic conversion defers it. Build it here, once, only if
// something genuinely needs it - an export without notes, or a PDF bundle.
- (NSData *)notesFreePDFData {
    if (mdFlags.pdfDataNeedsStripping) {
        mdFlags.pdfDataNeedsStripping = 0;
        PDFDocument *strippedDoc = [[PDFDocument alloc] initWithData:pdfData];
        [self tryToUnlockDocument:strippedDoc];
        for (PDFPage *page in strippedDoc) {
            for (PDFAnnotation *annotation in [[page annotations] copy]) {
                if ([annotation isSkimNote] == NO && [annotation isConvertibleAnnotation]) {
                    PDFAnnotation *popup = [annotation popup];
                    if (popup)
                        [page removeAnnotation:popup];
                    [page removeAnnotation:annotation];
                }
            }
        }
        NSData *stripped = [strippedDoc dataRepresentation];
        if (stripped)
            pdfData = stripped;
    }
    return pdfData;
}

- (BOOL)canAttachNotesForType:(NSString *)typeName {""",
     "- (NSData *)notesFreePDFData {"),

    # ---- read side: convert embedded annotations on open ------------------ #
    ("SKMainDocument.m",
     """    if (wasVisible == NO)
        [[NSNotificationCenter defaultCenter] postNotificationName:SKDocumentDidShowNotification object:self];
}""",
     """    if (wasVisible == NO)
        [[NSNotificationCenter defaultCenter] postNotificationName:SKDocumentDidShowNotification object:self];

    // Embedded annotations are read-only until converted. Converting them on
    // open is what makes highlights from other devices editable and lists them
    // in the notes pane. convertNotes replaces rather than duplicates them.
    if (wasVisible == NO && SKEmbedsNotesInPDF() && mdFlags.convertingNotes == 0 &&
        [self hasConvertibleAnnotations])
    {
        mdFlags.autoConvertingNotes = 1;
        [self convertNotes];
    }
}""",
     "mdFlags.autoConvertingNotes = 1;"),

    # opening a file is not an edit
    ("SKMainDocument.m",
     """- (void)updateChangeCount:(NSDocumentChangeType)change {
    if ((change & NSChangeDiscardable) == 0)
        [super updateChangeCount:change];
}""",
     """- (void)updateChangeCount:(NSDocumentChangeType)change {
    // the automatic convert-on-open is not a user edit; see showWindows
    if (mdFlags.autoConvertingNotes)
        return;
    if ((change & NSChangeDiscardable) == 0)
        [super updateChangeCount:change];
}""",
     "if (mdFlags.autoConvertingNotes)\n        return;"),

    # and must not hold the window while it runs
    ("SKMainDocument.m",
     '    [[self mainWindowController] beginProgressSheetWithMessage:[NSLocalizedString(@"Converting notes", @"Message for progress sheet") stringByAppendingEllipsis] maxValue:0];',
     '    if (mdFlags.autoConvertingNotes == 0)\n'
     '        [[self mainWindowController] beginProgressSheetWithMessage:[NSLocalizedString(@"Converting notes", @"Message for progress sheet") stringByAppendingEllipsis] maxValue:0];',
     "if (mdFlags.autoConvertingNotes == 0)\n        [[self mainWindowController] beginProgressSheet"),

    # ---- do not recompute pdfData: that is the destructive part ----------- #
    ("SKMainDocument.m",
     """    if (annotations) {
        
        dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{""",
     """    BOOL autoConverting = mdFlags.autoConvertingNotes != 0;
    
    if (annotations) {
        
        dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{""",
     "BOOL autoConverting = mdFlags.autoConvertingNotes != 0;"),

    ("SKMainDocument.m",
     """            // if pdfDocWithoutNotes was nil, the document was not encrypted, so no need to try to unlock
            PDFDocument *pdfDoc = pdfDocWithoutNotes ?: [[PDFDocument alloc] initWithData:pdfData];
            
            for (PDFPage *page in pdfDoc) {
                for (PDFAnnotation *annotation in [[page annotations] copy]) {
                    if ([annotation isSkimNote] == NO && [annotation isConvertibleAnnotation]) {
                        PDFAnnotation *popup = [annotation popup];
                        if (popup)
                            [page removeAnnotation:popup];
                        [page removeAnnotation:annotation];
                    }
                }
            }
            
            NSData *data = [pdfDoc dataRepresentation];""",
     """            NSData *data = nil;
            
            if (autoConverting == NO) {
                // if pdfDocWithoutNotes was nil, the document was not encrypted, so no need to try to unlock
                PDFDocument *pdfDoc = pdfDocWithoutNotes ?: [[PDFDocument alloc] initWithData:pdfData];
                
                for (PDFPage *page in pdfDoc) {
                    for (PDFAnnotation *annotation in [[page annotations] copy]) {
                        if ([annotation isSkimNote] == NO && [annotation isConvertibleAnnotation]) {
                            PDFAnnotation *popup = [annotation popup];
                            if (popup)
                                [page removeAnnotation:popup];
                            [page removeAnnotation:annotation];
                        }
                    }
                }
                
                data = [pdfDoc dataRepresentation];
            }""",
     "if (autoConverting == NO) {"),

    ("SKMainDocument.m",
     "                [self setPDFData:data pageOffsets:offsets];",
     """                if (data)
                    [self setPDFData:data pageOffsets:offsets];
                else
                    mdFlags.pdfDataNeedsStripping = 1;""",
     "mdFlags.pdfDataNeedsStripping = 1;"),

    ("SKMainDocument.m",
     """                [[self mainWindowController] dismissProgressSheet];
                
                mdFlags.convertingNotes = 0;""",
     """                if (mdFlags.autoConvertingNotes == 0)
                    [[self mainWindowController] dismissProgressSheet];
                
                mdFlags.convertingNotes = 0;
                
                if (mdFlags.autoConvertingNotes) {
                    // nothing here for the user to undo - they did not ask
                    mdFlags.autoConvertingNotes = 0;
                    [[self undoManager] removeAllActions];
                    [super updateChangeCount:NSChangeCleared];
                }""",
     "nothing here for the user to undo"),

    # ---- the two consumers that genuinely need notes-free data ------------ #
    ("SKMainDocument.m",
     '    [fileWrapper addRegularFileWithContents:pdfData preferredFilename:[name stringByAppendingPathExtension:@"pdf"]];',
     '    [fileWrapper addRegularFileWithContents:[self notesFreePDFData] preferredFilename:[name stringByAppendingPathExtension:@"pdf"]];',
     "addRegularFileWithContents:[self notesFreePDFData]"),

    ("SKMainDocument.m",
     "            didWrite = [pdfData writeToURL:absoluteURL options:0 error:&error];",
     """            // The ordinary save must write the original bytes untouched. Only
            // an explicit "without notes" export needs the stripped form, and
            // producing that costs the full PDFKit re-serialisation - which
            // doubles a scanned book and destroys its OCR word layer. Routing
            // the ordinary save through it put that damage straight back on
            // the save path, which is the one thing this design exists to
            // avoid.
            didWrite = [(mdFlags.exportOption == SKExportOptionWithoutNotes
                         ? [self notesFreePDFData] : pdfData)
                        writeToURL:absoluteURL options:0 error:&error];""",
     "? [self notesFreePDFData] : pdfData)"),

    # ---- handoff: graft the notes into the PDF once we are done with it --- #
    # On close rather than on save: the graft rewrites the file, and doing that
    # while Skim still holds the document makes its file-update checker prompt
    # to reload on every save. By close, Skim has written its final notes and
    # has no further claim on the file.
    ("SKMainDocument.m",
     "- (void)handleWindowWillCloseNotification:(NSNotification *)notification {",
     """- (void)runNoteGraftTool {
    NSString *tool = [[NSUserDefaults standardUserDefaults] stringForKey:SKNoteGraftToolKey];
    NSURL *fileURL = [self fileURL];
    if ([tool length] == 0 || fileURL == nil || [fileURL isFileURL] == NO)
        return;
    if ([[NSFileManager defaultManager] isExecutableFileAtPath:tool] == NO)
        return;
    // Our own graft changes the file while the document is open, so Skim's
    // update checker would ask to reload every time. Silence it for the
    // duration: re-enabling calls reset, which drops the stale modification
    // date and adopts the file as it now stands rather than reporting it.
    SKFileUpdateChecker *fuc = fileUpdateChecker;
    [fuc setEnabled:NO];
    NSTask *task = [[NSTask alloc] init];
    NSURL *grafted = fileURL;
    [task setTerminationHandler:^(NSTask *finished){
        dispatch_async(dispatch_get_main_queue(), ^{
            // NSDocument compares the file's modification date with the one it
            // recorded when saving. The graft changes the file after that, so
            // the next save reports a conflict - and answering "save anyway"
            // writes pdfData as loaded at open, throwing away what was
            // grafted. Adopting the new date is what stops both.
            // Take the grafted file as our own state. pdfData is what the
            // next save writes, and it is otherwise still the bytes loaded at
            // open - so every save put those back, undoing the graft, which
            // then slowly redid it. An "inert" save reverted the file.
            NSData *fresh = [NSData dataWithContentsOfURL:grafted];
            if (fresh)
                pdfData = fresh;
            NSDate *modified = [[[NSFileManager defaultManager]
                                 attributesOfItemAtPath:[grafted path] error:NULL]
                                fileModificationDate];
            if (modified)
                [self setFileModificationDate:modified];
            [fuc setEnabled:YES];
        });
    }];
    [task setExecutableURL:[NSURL fileURLWithPath:tool]];
    // --reconcile: the notes Skim just wrote are the complete state, so a
    // highlight deleted in Skim is deleted from the PDF. Without it the
    // tool only adds, and deletions would silently come back on reopen.
    // Save and close do the same thing. Add-only on save existed to dodge a
    // race - clearing the notes could wipe ones Skim wrote for a later edit -
    // but --clear-if-unchanged handles that directly, so the asymmetry only
    // made deletions behave differently from additions for no reason, and left
    // them dependent on the close hook firing.
    [task setArguments:@[[fileURL path], @"--reconcile", @"--clear-if-unchanged"]];
    // fire and forget: closing a document must not wait on it
    @try {
        [task launchAndReturnError:NULL];
    } @catch (NSException *e) {}
}

- (void)handleWindowWillCloseNotification:(NSNotification *)notification {
    if (SKEmbedsNotesInPDF())
        [self runNoteGraftTool];   // graft once the document is closed""",
     "[self setFileModificationDate:modified];"),
    # Grafting only on close leaves a long window in which the work exists only
    # in extended attributes: annotate for an hour, shut the laptop without
    # closing the document, and nothing has synced. Add-only after each save
    # closes that window and cannot race the next save's note write.
    ("SKMainDocument.m",
     """            if (NO == [self attachNotesAtURL:absoluteURL]) {""",
     """            if ([self attachNotesAtURL:absoluteURL]) {
                if (SKEmbedsNotesInPDF() &&
                    (saveOperation == NSSaveOperation || saveOperation == NSAutosaveInPlaceOperation))
                    [self runNoteGraftTool];   // graft after each save
            } else {""",
     "graft after each save"),

]

REQUIRED = [
    ("SKStringConstants.h", "extern NSString *SKNoteGraftToolKey;"),
    ("SKStringConstants.m", "NSString *SKNoteGraftToolKey ="),
    ("SKMainDocument.h", "unsigned int pdfDataNeedsStripping:1;"),
    ("SKMainDocument.m", "BOOL SKEmbedsNotesInPDF(void)"),
    ("SKMainDocument.m", "- (NSData *)notesFreePDFData {"),
    ("SKMainDocument.m", "mdFlags.autoConvertingNotes = 1;"),
    ("SKMainDocument.m", "if (mdFlags.autoConvertingNotes)\n        return;"),
    ("SKMainDocument.m", "if (autoConverting == NO) {"),
    ("SKMainDocument.m", "mdFlags.pdfDataNeedsStripping = 1;"),
    ("SKMainDocument.m", "addRegularFileWithContents:[self notesFreePDFData]"),
    ("SKMainDocument.m", "? [self notesFreePDFData] : pdfData)"),
    ("SKMainDocument.m", "[self setFileModificationDate:modified];"),
]

# these must NOT be present: they are the destructive edits we removed
FORBIDDEN = [
    ("SKMainDocument.m", "SKExportOptionDefault && SKEmbedsNotesInPDF()"),
    ("SKMainDocument.m", "BOOL stripNotes = attachNotes;"),
]


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    root = Path(sys.argv[1])
    revert = "--revert" in sys.argv
    if not (root / "SKMainDocument.m").exists():
        sys.exit(f"not a Skim checkout: {root}")

    # A marker must be something only this patch could produce. Twice now a
    # marker that already existed upstream made the patcher read pristine
    # source as already-patched and silently skip an edit, once leaving the
    # dirty-flag permanently stuck. Prove it cannot happen: every marker must
    # appear in its edit's replacement text and nowhere else.
    for rel, old, new, marker in EDITS:
        if marker not in new:
            print(f"  BUG   {rel}: marker is not produced by its own edit")
            return 1
        if marker in old:
            print(f"  BUG   {rel}: marker already present in the anchor text")
            return 1

    changed = skipped = 0
    for rel, old, new, marker in EDITS:
        p = root / rel
        s = p.read_text(encoding="utf-8")
        frm, to = (new, old) if revert else (old, new)
        if (marker in s) != revert:
            print(f"  skip  {rel}: already {'reverted' if revert else 'applied'}")
            skipped += 1
            continue
        if frm not in s:
            print(f"  FAIL  {rel}: anchor not found:\n        {frm.splitlines()[0][:76]}")
            return 1
        if s.count(frm) != 1:
            print(f"  FAIL  {rel}: anchor matches {s.count(frm)} times, expected 1")
            return 1
        p.write_text(s.replace(frm, to, 1), encoding="utf-8")
        print(f"  ok    {rel}: {'reverted' if revert else 'patched'}")
        changed += 1

    print(f"\n{changed} edit(s) applied, {skipped} already in place")

    if not revert:
        missing = [(f, t) for f, t in REQUIRED
                   if t not in (root / f).read_text(encoding="utf-8")]
        present = [(f, t) for f, t in FORBIDDEN
                   if t in (root / f).read_text(encoding="utf-8")]
        if missing or present:
            print("\nVERIFY FAILED:")
            for f, t in missing:
                print(f"  {f}: missing {t!r}")
            for f, t in present:
                print(f"  {f}: destructive edit still present: {t!r}")
            return 1
        print(f"verify: {len(REQUIRED)} changes present, "
              f"{len(FORBIDDEN)} destructive edits absent")
        print("\nEnable:")
        print("  defaults write net.sourceforge.skim-app.skim SKEmbedNotesInPDF -bool YES")
        print("  defaults write net.sourceforge.skim-app.skim SKNoteGraftTool "
              "-string ~/dev/syncthing-merge/skimgraft.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
