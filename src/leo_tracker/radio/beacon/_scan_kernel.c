/* Fused correlate-magnitude-fold kernel for Starlink edge-pilot acquisition.
 *
 * The reference NumPy path makes one pass over the signal for every
 * (frequency offset, anchor symbol) pair, materialising a full-length
 * correlation each time and then gathering it into frame epochs with fancy
 * indexing.  That is around 67 MB of memory traffic per probe to do 370 MFLOP
 * of arithmetic, so it runs an order of magnitude below what the machine can.
 *
 * Here correlation, magnitude and folding are fused: one kernel's intermediate
 * stays in L2 and the full-length correlation is never stored.  Three
 * properties keep the work small:
 *
 *   - |corr(x . e^-jwt, k)| == |corr(x, k . e^+jwm)|, so the 11-tap kernel
 *     carries the frequency hypothesis and the signal is never rotated.  The
 *     residual per-lag phase vanishes under the magnitude.
 *   - |x . e^-jwt|^2 == |x|^2, so the energy normaliser is independent of the
 *     frequency hypothesis and is computed once for the whole probe.
 *   - The tap loop runs outermost over a block of W lags held in registers, so
 *     each inner statement is a contiguous load times a broadcast scalar.  A
 *     split real/imaginary layout is used because an interleaved one compiles
 *     to LD2, whose deinterleave network occupies the same vector pipes that
 *     the multiply-accumulates need.
 *
 * Each kernel's fold start depends on which anchor symbol it was cut from, so
 * local_start is per kernel; passing a single shared start silently folds every
 * anchor onto the first anchor's epoch grid.
 */
#include <stddef.h>
#include <stdlib.h>
#ifdef _OPENMP
#include <omp.h>
#endif

#define LEO_BLOCK 12

/* xr, xi        split real/imaginary signal, n samples each
 * kr, ki        split kernel bank, k_count kernels of m taps
 * inv_knorm     reciprocal squared norm per kernel
 * inv_energy    reciprocal sliding power sum, length n - m + 1
 * local_start   per-kernel fold origin, the anchor symbol's sample offset
 * frame_offset  rint(f * frame_period) for f in [0, f_count)
 * agg           output, k_count * epochs doubles, caller-zeroed
 */
void leo_fold_correlate(const float *__restrict xr, const float *__restrict xi,
                        int n, const float *__restrict kr,
                        const float *__restrict ki, int k_count, int m,
                        const float *__restrict inv_knorm,
                        const float *__restrict inv_energy,
                        const int *__restrict local_start,
                        const int *__restrict frame_offset, int f_count,
                        int epochs, double *__restrict agg, int threads)
{
    const int lags = n - m + 1;
    if (lags <= 0) return;
#ifdef _OPENMP
    if (threads > 0) omp_set_num_threads(threads);
#endif
    #pragma omp parallel
    {
        float *scratch = (float *)malloc((size_t)lags * sizeof(float));
        if (scratch) {
            float re[LEO_BLOCK], im[LEO_BLOCK];
            #pragma omp for schedule(static)
            for (int k = 0; k < k_count; ++k) {
                const float *__restrict ar = kr + (size_t)k * m;
                const float *__restrict ai = ki + (size_t)k * m;
                const float scale = inv_knorm[k];

                int l = 0;
                for (; l + LEO_BLOCK <= lags; l += LEO_BLOCK) {
                    for (int w = 0; w < LEO_BLOCK; ++w) { re[w] = 0.0f; im[w] = 0.0f; }
                    for (int j = 0; j < m; ++j) {
                        const float cr = ar[j], cim = ai[j];
                        const float *__restrict pr = xr + l + j;
                        const float *__restrict pi = xi + l + j;
                        for (int w = 0; w < LEO_BLOCK; ++w) {
                            re[w] += pr[w] * cr + pi[w] * cim;
                            im[w] += pi[w] * cr - pr[w] * cim;
                        }
                    }
                    for (int w = 0; w < LEO_BLOCK; ++w)
                        scratch[l + w] = (re[w] * re[w] + im[w] * im[w])
                                       * inv_energy[l + w] * scale;
                }
                for (; l < lags; ++l) {
                    float a = 0.0f, b = 0.0f;
                    for (int j = 0; j < m; ++j) {
                        const float pr = xr[l + j], pi = xi[l + j];
                        a += pr * ar[j] + pi * ai[j];
                        b += pi * ar[j] - pr * ai[j];
                    }
                    scratch[l] = (a * a + b * b) * inv_energy[l] * scale;
                }

                /* Fold contiguous frame-length runs from this kernel's own
                 * anchor origin rather than gathering scattered indices. */
                double *__restrict out = agg + (size_t)k * epochs;
                const int base = local_start[k];
                for (int f = 0; f < f_count; ++f) {
                    const int s = base + frame_offset[f];
                    if (s >= lags || s < 0) continue;
                    int count = lags - s;
                    if (count > epochs) count = epochs;
                    const float *__restrict src = scratch + s;
                    for (int e = 0; e < count; ++e) out[e] += (double)src[e];
                }
            }
            free(scratch);
        }
    }
}

/* Sliding |x|^2 over an m-sample window, computed once per probe because it
 * does not depend on the frequency hypothesis. */
void leo_sliding_energy(const float *__restrict xr, const float *__restrict xi,
                        int n, int m, double *__restrict energy)
{
    const int lags = n - m + 1;
    if (lags <= 0) return;
    double total = 0.0;
    for (int j = 0; j < m; ++j)
        total += (double)xr[j] * xr[j] + (double)xi[j] * xi[j];
    energy[0] = total;
    for (int l = 1; l < lags; ++l) {
        const int drop = l - 1, add = l + m - 1;
        total -= (double)xr[drop] * xr[drop] + (double)xi[drop] * xi[drop];
        total += (double)xr[add] * xr[add] + (double)xi[add] * xi[add];
        energy[l] = total;
    }
}
