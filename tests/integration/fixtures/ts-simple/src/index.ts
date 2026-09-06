import { add, Vector } from "./utils";

export class Calculator {
  sum(a: number, b: number): number {
    return add(a, b);
  }
}

export function origin(): Vector {
  return { x: 0, y: 0 };
}
