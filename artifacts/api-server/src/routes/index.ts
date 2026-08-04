import { Router, type IRouter } from "express";
import healthRouter from "./health";
import upiRouter from "./upi";

const router: IRouter = Router();

router.use(healthRouter);
router.use(upiRouter);

export default router;
