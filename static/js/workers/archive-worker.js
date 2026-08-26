/**
 * Chemistry Lab - Client-Side Archive & File Processing Web Worker
 * ===============================================================
 * Offloads archive extraction (ZIP, TAR, GZ), SHA-256 hashing, and
 * 6-tier file classification from the main UI thread to prevent UI freezes.
 *
 * Mandatory limits & safety:
 * - Configurable limits for archive size, extracted size, file count, and file size.
 * - Zip-Slip / path traversal protection.
 * - Decompression bomb ratio protection.
 * - Web Crypto SHA-256 deduplication.
 * - 6-tier classification: ANALYZE, KEEP, IGNORE, DUPLICATE, UNKNOWN, UNSUPPORTED.
 * - UNKNOWN never means delete (it is preserved for user inspection).
 */

const DEFAULT_CONFIG = {
    maxArchiveSizeBytes: 500 * 1024 * 1024,      // 500 MB
    maxExtractedTotalBytes: 1024 * 1024 * 1024,  // 1 GB
    maxIndividualFileSizeBytes: 250 * 1024 * 1024, // 250 MB
    maxFileCount: 2000,
    maxCompressionRatio: 100.0
};

// Supported chemistry formats for the active chemistry engine
const ANALYZE_EXTENSIONS = new Set(['.out', '.log', '.xyz', '.molden', '.sdf', '.mol']);
const KEEP_EXTENSIONS = new Set(['.inp', '.hess', '.allxyz', '.pdb', '.cube', '.dat', '.property.txt']);

const IGNORE_PATTERNS = [
    /^__macosx\//i,
    /\.ds_store$/i,
    /thumbs\.db$/i,
    /^desktop\.ini$/i,
    /\.git\//i,
    /\.pyc$/i,
    /~$/,
    /\.bak$/i,
    /\.tmp$/i
];
const UNSUPPORTED_BINARIES = new Set([
    '.exe', '.dll', '.so', '.dylib', '.bin', '.iso', '.msi',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.mp4', '.avi', '.mov', '.pdf', '.docx'
]);

/**
 * Sanitize relative paths to prevent Zip Slip / directory traversal.
 */
function sanitizePath(rawPath) {
    if (!rawPath) return '';
    // Normalize slashes
    let p = rawPath.replace(/\\/g, '/');
    // Remove drive letters (e.g. C:)
    p = p.replace(/^[a-zA-Z]:/, '');
    // Split and filter parts
    const parts = p.split('/').filter(Boolean);
    const safeParts = [];
    for (const part of parts) {
        if (part === '.' || part === '') continue;
        if (part === '..') {
            // Prevent traversal outside root
            if (safeParts.length > 0) safeParts.pop();
        } else {
            safeParts.push(part);
        }
    }
    return safeParts.join('/');
}

/**
 * Compute SHA-256 hex digest for ArrayBuffer/Uint8Array
 */
async function computeSha256(uint8Arr) {
    try {
        const hashBuffer = await crypto.subtle.digest('SHA-256', uint8Arr);
        const hashArray = Array.from(new Uint8Array(hashBuffer));
        return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
    } catch (e) {
        return 'hash_unavailable';
    }
}

/**
 * Classify a file entry based on name, size, and content
 */
function classifyFile(filename, size, isDuplicate = false) {
    const cleanName = sanitizePath(filename);
    const lowerName = cleanName.toLowerCase();
    const basename = cleanName.split('/').pop() || '';
    const lowerBase = basename.toLowerCase();

    if (isDuplicate) {
        return {
            tier: 'DUPLICATE',
            reason: 'Identical file content detected via SHA-256 digest; redundant copy excluded from calculation queue.'
        };
    }

    for (const pattern of IGNORE_PATTERNS) {
        if (pattern.test(cleanName) || pattern.test(basename)) {
            return {
                tier: 'IGNORE',
                reason: 'OS/system metadata or temporary file excluded from scientific analysis.'
            };
        }
    }

    if (lowerBase.startsWith('readme') || lowerBase.startsWith('license')) {
        return {
            tier: 'IGNORE',
            reason: 'Documentation/license text excluded from quantum chemistry queue.'
        };
    }

    const dotIdx = lowerBase.lastIndexOf('.');
    const ext = dotIdx !== -1 ? lowerBase.substring(dotIdx) : '';

    if (lowerBase.endsWith('.property.txt')) {
        return {
            tier: 'ANALYZE',
            reason: 'ORCA property output file recognized by analytical engine.'
        };
    }

    if (ANALYZE_EXTENSIONS.has(ext)) {
        return {
            tier: 'ANALYZE',
            reason: `Supported chemistry file format (${ext.toUpperCase()}) ready for property and geometry extraction.`
        };
    }

    if (KEEP_EXTENSIONS.has(ext)) {
        return {
            tier: 'KEEP',
            reason: `Scientific ancillary calculation file (${ext.toUpperCase()}) preserved in dataset.`
        };
    }

    if (UNSUPPORTED_BINARIES.has(ext)) {
        return {
            tier: 'UNSUPPORTED',
            reason: `Non-computational binary or media format (${ext.toUpperCase()}) not supported for quantum analysis.`
        };
    }

    return {
        tier: 'UNKNOWN',
        reason: 'Unrecognized file extension; preserved for user inspection and manual selection.'
    };
}

/**
 * Pure JS ZIP Reader using DecompressionStream for DEFLATE
 */
async function processZipBuffer(arrayBuffer, config, reportProgress) {
    const view = new DataView(arrayBuffer);
    const uint8 = new Uint8Array(arrayBuffer);
    const totalBytes = arrayBuffer.byteLength;

    if (totalBytes > config.maxArchiveSizeBytes) {
        throw new Error(`Archive size (${(totalBytes / 1024 / 1024).toFixed(1)} MB) exceeds maximum allowed limit of ${(config.maxArchiveSizeBytes / 1024 / 1024).toFixed(0)} MB.`);
    }

    const entries = [];
    let extractedTotalBytes = 0;
    const seenHashes = new Map();

    // 1. Locate End of Central Directory Record (EOCD)
    let eocdOffset = -1;
    for (let i = totalBytes - 22; i >= Math.max(0, totalBytes - 65557); i--) {
        if (view.getUint32(i, true) === 0x06054b50) {
            eocdOffset = i;
            break;
        }
    }

    if (eocdOffset === -1) {
        throw new Error('Invalid or corrupted ZIP archive: End of Central Directory record not found.');
    }

    const entryCount = view.getUint16(eocdOffset + 10, true);
    const cdOffset = view.getUint32(eocdOffset + 16, true);

    if (entryCount > config.maxFileCount) {
        throw new Error(`ZIP contains ${entryCount} files, exceeding max limit of ${config.maxFileCount}.`);
    }

    let offset = cdOffset;
    const decoder = new TextDecoder('utf-8');

    for (let idx = 0; idx < entryCount; idx++) {
        if (offset + 46 > totalBytes) break;
        if (view.getUint32(offset, true) !== 0x02014b50) break;

        const method = view.getUint16(offset + 10, true);
        const compSize = view.getUint32(offset + 20, true);
        const uncompSize = view.getUint32(offset + 24, true);
        const nameLen = view.getUint16(offset + 28, true);
        const extraLen = view.getUint16(offset + 30, true);
        const commentLen = view.getUint16(offset + 32, true);
        const localHeaderOffset = view.getUint32(offset + 42, true);

        const nameBytes = uint8.subarray(offset + 46, offset + 46 + nameLen);
        const rawFilename = decoder.decode(nameBytes);
        const cleanFilename = sanitizePath(rawFilename);

        offset += 46 + nameLen + extraLen + commentLen;

        // Skip directory entries
        if (rawFilename.endsWith('/') || rawFilename.endsWith('\\') || !cleanFilename) {
            continue;
        }

        // Check ratio
        if (compSize > 0 && (uncompSize / compSize) > config.maxCompressionRatio) {
            throw new Error(`Suspicious compression ratio (${(uncompSize / compSize).toFixed(1)}x) for "${cleanFilename}". Possible decompression bomb.`);
        }

        if (uncompSize > config.maxIndividualFileSizeBytes) {
            throw new Error(`File "${cleanFilename}" (${(uncompSize / 1024 / 1024).toFixed(1)} MB) exceeds single-file limit of ${(config.maxIndividualFileSizeBytes / 1024 / 1024).toFixed(0)} MB.`);
        }

        extractedTotalBytes += uncompSize;
        if (extractedTotalBytes > config.maxExtractedTotalBytes) {
            throw new Error(`Total extracted size exceeds ${(config.maxExtractedTotalBytes / 1024 / 1024).toFixed(0)} MB limit.`);
        }

        // Read Local File Header to find data start
        if (localHeaderOffset + 30 > totalBytes || view.getUint32(localHeaderOffset, true) !== 0x04034b50) {
            continue;
        }
        const localNameLen = view.getUint16(localHeaderOffset + 26, true);
        const localExtraLen = view.getUint16(localHeaderOffset + 28, true);
        const dataStart = localHeaderOffset + 30 + localNameLen + localExtraLen;
        const compressedData = uint8.subarray(dataStart, dataStart + compSize);

        let fileData;
        if (method === 0) {
            // Stored (no compression)
            fileData = compressedData.slice();
        } else if (method === 8) {
            // DEFLATE
            try {
                if (typeof DecompressionStream !== 'undefined') {
                    const ds = new DecompressionStream('deflate-raw');
                    const writer = ds.writable.getWriter();
                    writer.write(compressedData);
                    writer.close();
                    const chunks = [];
                    const reader = ds.readable.getReader();
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        chunks.push(value);
                    }
                    const totalLen = chunks.reduce((acc, c) => acc + c.length, 0);
                    fileData = new Uint8Array(totalLen);
                    let pos = 0;
                    for (const c of chunks) {
                        fileData.set(c, pos);
                        pos += c.length;
                    }
                } else {
                    fileData = compressedData;
                }
            } catch (decompErr) {
                console.warn(`Decompression failed for ${cleanFilename}:`, decompErr);
                continue;
            }
        } else {
            // Unsupported compression method
            continue;
        }

        const sha256 = await computeSha256(fileData);
        const isDuplicate = seenHashes.has(sha256);
        if (!isDuplicate) {
            seenHashes.set(sha256, cleanFilename);
        }

        const classification = classifyFile(cleanFilename, uncompSize, isDuplicate);
        
        let textContent = null;
        if (classification.tier === 'ANALYZE' || classification.tier === 'KEEP' || classification.tier === 'UNKNOWN') {
            try {
                textContent = decoder.decode(fileData);
            } catch (e) {
                textContent = null;
            }
        }

        entries.push({
            name: cleanFilename,
            size: uncompSize,
            compressedSize: compSize,
            sha256: sha256,
            tier: classification.tier,
            reason: classification.reason,
            duplicateOf: isDuplicate ? seenHashes.get(sha256) : null,
            content: textContent
        });

        if (reportProgress) {
            reportProgress(idx + 1, entryCount, cleanFilename);
        }
    }

    return entries;
}

/**
 * TAR Archive Reader with optional GZIP decompression
 */
async function processTarBuffer(arrayBuffer, config, reportProgress) {
    let tarBytes = new Uint8Array(arrayBuffer);

    // Test for GZIP header (0x1F, 0x8B)
    if (tarBytes.length > 2 && tarBytes[0] === 0x1F && tarBytes[1] === 0x8B) {
        if (typeof DecompressionStream !== 'undefined') {
            const ds = new DecompressionStream('gzip');
            const writer = ds.writable.getWriter();
            writer.write(tarBytes);
            writer.close();
            const chunks = [];
            const reader = ds.readable.getReader();
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                chunks.push(value);
            }
            const totalLen = chunks.reduce((acc, c) => acc + c.length, 0);
            const decomp = new Uint8Array(totalLen);
            let pos = 0;
            for (const c of chunks) {
                decomp.set(c, pos);
                pos += c.length;
            }
            tarBytes = decomp;
        }
    }

    const entries = [];
    let offset = 0;
    const decoder = new TextDecoder('utf-8');
    const seenHashes = new Map();
    let extractedTotalBytes = 0;

    while (offset + 512 <= tarBytes.length) {
        const header = tarBytes.subarray(offset, offset + 512);
        // Check for empty block
        if (header.every(b => b === 0)) {
            offset += 512;
            break;
        }

        let rawName = decoder.decode(header.subarray(0, 100)).replace(/\0.*$/, '').trim();
        const sizeStr = decoder.decode(header.subarray(124, 136)).replace(/\0.*$/, '').trim();
        const typeFlag = String.fromCharCode(header[156]);
        const size = parseInt(sizeStr, 8) || 0;

        offset += 512;
        const cleanName = sanitizePath(rawName);

        if (typeFlag === '0' || typeFlag === '\0') {
            if (cleanName && !cleanName.endsWith('/')) {
                if (size > config.maxIndividualFileSizeBytes) {
                    throw new Error(`File "${cleanName}" (${(size / 1024 / 1024).toFixed(1)} MB) exceeds size limit.`);
                }
                extractedTotalBytes += size;
                if (extractedTotalBytes > config.maxExtractedTotalBytes) {
                    throw new Error(`Total extracted size exceeds limit.`);
                }

                const fileData = tarBytes.subarray(offset, offset + size);
                const sha256 = await computeSha256(fileData);
                const isDuplicate = seenHashes.has(sha256);
                if (!isDuplicate) seenHashes.set(sha256, cleanName);

                const classification = classifyFile(cleanName, size, isDuplicate);
                let textContent = null;
                if (classification.tier === 'ANALYZE' || classification.tier === 'KEEP' || classification.tier === 'UNKNOWN') {
                    try { textContent = decoder.decode(fileData); } catch (e) {}
                }

                entries.push({
                    name: cleanName,
                    size: size,
                    sha256: sha256,
                    tier: classification.tier,
                    reason: classification.reason,
                    duplicateOf: isDuplicate ? seenHashes.get(sha256) : null,
                    content: textContent
                });

                if (reportProgress) {
                    reportProgress(entries.length, entries.length + 1, cleanName);
                }
            }
        }

        offset += Math.ceil(size / 512) * 512;
    }

    return entries;
}

// -------------------------------------------------------------
// Message Listener
// -------------------------------------------------------------
self.onmessage = async function(e) {
    const data = e.data || {};
    const { action, buffer, filename, config: userConfig, rawFiles } = data;
    const config = Object.assign({}, DEFAULT_CONFIG, userConfig || {});

    try {
        if (action === 'PROCESS_ARCHIVE') {
            const isTar = /\.tar(\.gz|\.bz2|\.xz)?$/i.test(filename) || /\.tgz$/i.test(filename);
            const reportProgress = (current, total, name) => {
                self.postMessage({
                    type: 'PROGRESS',
                    current: current,
                    total: total,
                    filename: name,
                    percent: Math.min(100, Math.round((current / (total || 1)) * 100))
                });
            };

            const entries = isTar 
                ? await processTarBuffer(buffer, config, reportProgress)
                : await processZipBuffer(buffer, config, reportProgress);

            const summary = {
                total: entries.length,
                analyze: entries.filter(f => f.tier === 'ANALYZE').length,
                keep: entries.filter(f => f.tier === 'KEEP').length,
                ignore: entries.filter(f => f.tier === 'IGNORE').length,
                duplicate: entries.filter(f => f.tier === 'DUPLICATE').length,
                unknown: entries.filter(f => f.tier === 'UNKNOWN').length,
                unsupported: entries.filter(f => f.tier === 'UNSUPPORTED').length
            };

            self.postMessage({
                type: 'COMPLETE',
                success: true,
                archiveName: filename,
                entries: entries,
                summary: summary
            });
        } else if (action === 'PROCESS_PLAIN_FILES') {
            let entries = [];
            const seenHashes = new Map();
            const decoder = new TextDecoder('utf-8');

            const reportProgress = (current, total, name) => {
                self.postMessage({
                    type: 'PROGRESS',
                    current: current,
                    total: total,
                    filename: name,
                    percent: Math.min(100, Math.round((current / (total || 1)) * 100))
                });
            };

            for (let i = 0; i < (rawFiles || []).length; i++) {
                const file = rawFiles[i];
                const isArchive = /\.(zip|tar|gz|tgz|tar\.gz|tar\.bz2|tar\.xz)$/i.test(file.name);

                if (isArchive) {
                    const isTar = /\.tar(\.gz|\.bz2|\.xz)?$/i.test(file.name) || /\.tgz$/i.test(file.name);
                    const subEntries = isTar
                        ? await processTarBuffer(file.buffer, config, reportProgress)
                        : await processZipBuffer(file.buffer, config, reportProgress);

                    for (const sub of subEntries) {
                        const isDup = seenHashes.has(sub.sha256);
                        if (!isDup) seenHashes.set(sub.sha256, `${file.name}/${sub.name}`);
                        else {
                            sub.tier = 'DUPLICATE';
                            sub.duplicateOf = seenHashes.get(sub.sha256);
                        }
                        entries.push(sub);
                    }
                } else {
                    const uint8 = new Uint8Array(file.buffer);
                    const sha256 = await computeSha256(uint8);
                    const isDuplicate = seenHashes.has(sha256);
                    if (!isDuplicate) seenHashes.set(sha256, file.name);

                    const classification = classifyFile(file.name, file.size, isDuplicate);
                    let textContent = null;
                    if (classification.tier === 'ANALYZE' || classification.tier === 'KEEP' || classification.tier === 'UNKNOWN') {
                        try { textContent = decoder.decode(uint8); } catch (e) {}
                    }

                    entries.push({
                        name: file.name,
                        size: file.size,
                        sha256: sha256,
                        tier: classification.tier,
                        reason: classification.reason,
                        duplicateOf: isDuplicate ? seenHashes.get(sha256) : null,
                        content: textContent
                    });
                }

                self.postMessage({
                    type: 'PROGRESS',
                    current: i + 1,
                    total: rawFiles.length,
                    filename: file.name,
                    percent: Math.round(((i + 1) / rawFiles.length) * 100)
                });
            }

            const summary = {
                total: entries.length,
                analyze: entries.filter(f => f.tier === 'ANALYZE').length,
                keep: entries.filter(f => f.tier === 'KEEP').length,
                ignore: entries.filter(f => f.tier === 'IGNORE').length,
                duplicate: entries.filter(f => f.tier === 'DUPLICATE').length,
                unknown: entries.filter(f => f.tier === 'UNKNOWN').length,
                unsupported: entries.filter(f => f.tier === 'UNSUPPORTED').length
            };

            self.postMessage({
                type: 'COMPLETE',
                success: true,
                entries: entries,
                summary: summary
            });
        }
    } catch (err) {
        self.postMessage({
            type: 'ERROR',
            success: false,
            error: err.message || String(err)
        });
    }
};
