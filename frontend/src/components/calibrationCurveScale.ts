/** Display-only compression in counts. Zero, sign and every finite value survive. */
export const signedLog = (value:number) => Math.sign(value)*Math.log1p(Math.abs(value))/Math.LN10;
export const inverseSignedLog = (value:number) => Math.sign(value)*Math.expm1(Math.abs(value)*Math.LN10);
