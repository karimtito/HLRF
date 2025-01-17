from typing import Union, Tuple, Any, Optional
import torch
from ..criteria import Misclassification
from ..criteria import TargetedMisclassification
from .base import MinimizationAttack
from .base import T
from .base import raise_if_kwargs
from .base import get_criterion
from .base import verify_input_bounds
from ..distances import l2
from ..models import Model
import eagerpy as ep
import numpy as np
from ..devutils import flatten

class BPArmijo(MinimizationAttack):

	distance = l2

	def __init__(self,
				steps: int = 50,
				gamma: float = 0.3,
				num_classes: int =1000,
				targeted: bool = False,
				confidence: float = 0.1,):
		self.steps = steps
		self.gamma = gamma
		self.num_classes = num_classes
		self.query_count: int = 0
		self.targeted = targeted
		self.confidence = confidence
		

	def gamma_step(self, current_step):
		rate = current_step/(self.steps+1.0)
		rage = 1-self.gamma
		epsi = self.gamma + rate*rage
		return epsi

	def classif_loss(self,
					image: ep.Tensor,
					labels: ep.Tensor,
					model: Model,
		) -> Tuple[ep.Tensor, Tuple[ep.Tensor,ep.Tensor]]:

			"""
			The loss used to study adverariality. If <0, then the image is adversarial
			"""
			row = len(labels)
			rows = range(row)
			self.query_count += 1
			mod = model(image)
			logits = ep.softmax(mod)

			if self.targeted:
				c_minimize = self.best_other_classes(logits, labels)
				c_maximize = labels  # target_classes
			else:
				c_minimize = labels  # labels
				c_maximize = self.best_other_classes(logits, labels)

			loss = (
				logits[rows, c_minimize] - logits[rows, c_maximize]
			) + self.confidence

			return loss.sum(), (loss,logits[rows, self.best_other_classes(logits, labels)])

	def best_other_classes(self,logits: ep.Tensor, exclude: ep.Tensor) -> ep.Tensor:
		other_logits = logits - ep.onehot_like(logits, exclude, value=10e6)
		res = other_logits.argmax(axis=-1)

		return res

	def normalize(self, tensor_to_normalize):
		tensor_norm = l2(tensor_to_normalize,ep.zeros_like(tensor_to_normalize))
		#Prevent inf / nan errors
		tensor_norm = ep.where(tensor_norm==0,ep.ones_like(tensor_norm),tensor_norm)
		normalized_tensor = tensor_to_normalize/tensor_norm
		return(normalized_tensor, tensor_norm)
	
	def calculate_factor(self, loss, grad_norm,ratio):
		steps_ratio = self.steps*ratio
		decreasing_term = 1/(steps_ratio*self.gamma+(steps_ratio*(steps_ratio-1))/2*(1-self.gamma)/(self.steps+1))
		return(loss/grad_norm*decreasing_term)

	def merit_func(self,inputs_,modif,loss,sigma):
		return 0.5 * ep.norms.l2(flatten(inputs_-modif), -1)**2 + sigma * loss ** 2

	def grad_merit(self,inputs_,modif,loss,sigma,grad):
		return modif - inputs_ + 2 * sigma * loss * grad

	def armijo(self,model,inputs,direction,grad_x,adv,loss,label,omega=1e-4,beta=0.5,max_iter=10,smooth=2):
		optimal_direction = ep.zeros_like(direction)
		respect_armijo = [False for i in range(self.batch_size)]
		norm_adv = ep.norms.l2(flatten(adv-inputs), -1)
		grad_norm = ep.norms.l2(flatten(grad_x), -1)
		grad_norm = ep.where(grad_norm == 0, 1, grad_norm)
		sigma = smooth * ep.maximum( norm_adv / grad_norm,0.5 * ep.norms.l2(flatten(adv+direction-inputs), -1)**2 / ep.abs(loss))
		merit = self.merit_func(inputs,adv,loss,sigma)
		grad_merit = self.grad_merit(inputs,adv,loss,sigma,grad_x)
		condition = merit + omega * (direction * grad_merit).sum(axis=(1,2,3))
		k = 0
		already_respect_armijo = [0]*len(respect_armijo)
		while not (all(respect_armijo)) and k < max_iter:
			k += 1
			_, (loss,_) = self.classif_loss(adv + direction * (beta**k),labels=label,model=model)
			merit_k = self.merit_func(inputs,adv + direction * (beta**k),loss,sigma)
			respect_armijo = merit_k <= condition
			optimal_direction = [direction if already_respect_armijo else direction * beta**k]
			already_respect_armijo = [already_respect_armijo or respect_armijo]
		return ep.stack(optimal_direction[0])




	def run(self,
			model: Model,
			inputs_: T,
			criterion: Union[Misclassification, TargetedMisclassification, T],
			early_stop: bool = False,
			**kwargs: Any,
			) -> T:

		raise_if_kwargs(kwargs)
		# create a list of nb of queries for each image
		#self.query_count = [0] * len(inputs_)
		inputs, restore_type = ep.astensor_(inputs_)
		criterion_ = get_criterion(criterion)
		del inputs_, criterion, kwargs
		verify_input_bounds(inputs, model)

		N = len(inputs)

		if isinstance(criterion_, Misclassification):
			targeted = False
			labels = criterion_.labels

		elif isinstance(criterion_, TargetedMisclassification):
			targeted = True
			labels = criterion_.target_classes

		loss_aux_and_grad = ep.value_and_grad_fn(ep.zeros_like(inputs), self.classif_loss,has_aux=True)
		
		batch_size = inputs.shape[0]
		self.batch_size = batch_size
		multiplier = 1 if targeted else -1
		best_adv = inputs
		best_norm = (ep.ones_like(inputs[0])*1e6)
		adv = inputs
		_ , (adversarial_loss,pred_labels),grad = loss_aux_and_grad(adv, labels,model)
		normalized_grad, grad_norm = self.normalize(grad)

		#Approximate the factor required to end Stage 1 within a ratio of the total steps
		ratio_factor =  self.calculate_factor(adversarial_loss, grad_norm, ratio=0.2)
		is_adv = (pred_labels!=labels)
		already_adversarial = is_adv
		i=0
		ever_found = already_adversarial #help control stage 1 in case an image is already adversarial
		while i<self.steps:
			#Update Gamma
			gammas = self.gamma_step(i)

			#Stage 1: finding an adversarial sample quickly
			adv_stage_1 = adv + ratio_factor*multiplier*gammas*normalized_grad
			#torch.clamp(input = adv_stage_1,min = 0,max = 255, out=adv_stage_1)

			#Stage 2
			perturbation = adv-inputs
			normalized_perturbation, perturbation_norm = self.normalize(perturbation)
			#Projection of the perturbation onto the gradient vector
			proj_perturbation = (normalized_perturbation*-normalized_grad)

			#Case OUT
			# samples that are still adversarial  -> decrease distortion
			epsilons_out = gammas*perturbation_norm
			v_star = inputs + multiplier*proj_perturbation*normalized_grad
			v_adv_diff = (adv-v_star)
			diff_normed, diff_norm = self.normalize(v_adv_diff)
			distortion_control_out = epsilons_out**2-proj_perturbation**2
			distortion_control_out = ep.maximum(ep.zeros_like(distortion_control_out), distortion_control_out)
			adv_stage_2_out = v_star + diff_normed*(distortion_control_out**0.5)
			direction = adv_stage_2_out-adv
			adv_stage_2_out = self.armijo(model = model, inputs = inputs, direction = direction, grad_x = grad , adv = adv_stage_2_out, loss = adversarial_loss, label = labels)


			#Case IN
			# samples that are not adversarial anymore -> increase distortion
			epsilons_in = perturbation_norm/gammas
			distortion_control_in = epsilons_in**2-perturbation_norm**2+proj_perturbation**2
			adv_stage_2_in = adv + multiplier*(proj_perturbation+distortion_control_in**0.5)*normalized_grad
			#Keep the right adversarial w.r.t. IN/OUT case
			adv_stage_2 = ep.where(is_adv,adv_stage_2_out,adv_stage_2_in)

			#If stage 1 has ever been succesful, keep stage 2. Keep stage 1 otherwise
			new_adv = adv_stage_1*(ever_found==0) + adv_stage_2*(ever_found!=0)
			#new_adv = torch.clamp(new_adv,0,255)
			#Update grads, current adversarial samples
			adv = new_adv
			_ , (adversarial_loss,pred_labels),grad = loss_aux_and_grad(adv, labels, model)
			normalized_grad, grad_norm = self.normalize(grad)
			is_adv = (pred_labels!=labels)
			ever_found = ever_found+is_adv.float32()
			
			better_norm = perturbation_norm<=best_norm
			better_norm_and_adv = better_norm*is_adv
			best_adv = ep.where(better_norm_and_adv,adv,best_adv)
			best_norm = ep.norms.l2(flatten(best_adv), -1)

			i+=1


		best_adv = ep.where(already_adversarial,inputs,best_adv)
		return restore_type(best_adv)

class BP(MinimizationAttack):

	distance = l2
	def __init__(self,
				steps: int = 50,
				gamma: float = 0.3,
				num_classes: int =1000,
				targeted: bool = False,):
		self.targeted = targeted
		self.steps = steps
		self.gamma = gamma
		self.num_classes = num_classes
		self.query_count = 0
		

	def gamma_step(self, current_step):
		rate = current_step/(self.steps+1.0)
		rage = 1-self.gamma
		epsi = self.gamma + rate*rage
		return epsi

	def classif_loss(self,
					image: ep.Tensor,
					labels: ep.Tensor,
					model: Model,
		) -> Tuple[ep.Tensor, Tuple[ep.Tensor,ep.Tensor]]:

			"""
			The loss used to study adverariality. If <0, then the image is adversarial
			"""
			row = len(labels)
			rows = range(row)
			self.query_count += 1
			mod = model(image)
			logits = ep.softmax(mod)

			if self.targeted:
				c_minimize = self.best_other_classes(logits, labels)
				c_maximize = labels  # target_classes
			else:
				c_minimize = labels  # labels
				c_maximize = self.best_other_classes(logits, labels)

			loss = (
				logits[rows, c_minimize] - logits[rows, c_maximize]
			) + self.confidence

			return loss.sum(), (loss,logits[rows, self.best_other_classes(logits, labels)])

	def best_other_classes(logits: ep.Tensor, exclude: ep.Tensor) -> ep.Tensor:
		other_logits = logits - ep.onehot_like(logits, exclude, value=10e6)
		res = other_logits.argmax(axis=-1)

		return res

	def normalize(self, tensor_to_normalize):
		tensor_norm = ep.norms.l2(flatten(tensor_to_normalize), -1)
		#Prevent inf / nan errors
		tensor_norm = ep.where(tensor_norm==0, ep.ones_like(tensor_norm), tensor_norm)
		normalized_tensor = tensor_to_normalize/tensor_norm
		return(normalized_tensor, tensor_norm)
	
	def calculate_factor(self, loss, grad_norm,ratio):
		steps_ratio = self.steps*ratio
		decreasing_term = 1/(steps_ratio*self.gamma+(steps_ratio*(steps_ratio-1))/2*(1-self.gamma)/(self.steps+1))
		return(loss/grad_norm*decreasing_term)

	def merit_func(self,inputs_,modif,loss,sigma):
		return 0.5 * ep.norms.l2(flatten(inputs_-modif), -1)**2 + sigma * loss ** 2

	def grad_merit(self,inputs_,modif,loss,sigma,grad):
		return modif - inputs_ + 2 * sigma * loss * grad

	def armijo(self,model,inputs,direction,grad_x,adv,loss,omega=1e-4,beta=0.5,max_iter=10):
		optimal_direction = ep.zeros_like(direction)
		respect_armijo = [False for i in range(self.batch_size)]
		norm_adv = ep.norms.l2(flatten(adv-inputs), -1)
		grad_norm = ep.norms.l2(flatten(grad_x), -1)
		grad_norm = ep.where(grad_norm == 0, 1, grad_norm)
		sigma = self.smooth * ep.maximum( norm_adv / grad_norm,0.5 * ep.norms.l2(flatten(adv+direction-inputs), -1)**2 / ep.abs(loss))
		merit = self.merit_func(inputs,adv,loss,sigma)
		grad_merit = self.grad_merit(inputs,adv,loss,sigma,grad_x)
		condition = merit + omega * (direction * grad_merit).sum(axis=(1,2,3))
		k = 0
		already_respect_armijo = ep.zeros_like(respect_armijo)
		while not ep.forall(respect_armijo) and k < max_iter:
			k += 1
			_, loss = self.classif_loss(model,adv + direction * (beta**k))[2]
			merit_k = self.merit_func(inputs,adv + direction * (beta**k),loss,sigma)
			respect_armijo = merit_k <= condition
			optimal_direction = ep.where(already_respect_armijo, direction, direction * beta**k)
			already_respect_armijo = already_respect_armijo | respect_armijo
		return optimal_direction




	def run(self,
			model: Model,
			inputs_: T,
			criterion: Union[Misclassification, TargetedMisclassification, T],
			early_stop: bool = None,
			**kwargs: Any,
			) -> T:

		raise_if_kwargs(kwargs)
		# create a list of nb of queries for each image
		#self.query_count = [0] * len(inputs_)
		inputs, restore_type = ep.astensor_(inputs_)
		criterion_ = get_criterion(criterion)
		del inputs_, criterion, kwargs
		verify_input_bounds(inputs, model)

		N = len(inputs)

		if isinstance(criterion_, Misclassification):
			targeted = False
			labels = criterion_.labels

		elif isinstance(criterion_, TargetedMisclassification):
			targeted = True
			labels = criterion_.target_classes

		loss_aux_and_grad = ep.value_and_grad_fn(self.classif_loss,has_aux=True)
		
		batch_size = inputs.shape[0]
		self.batch_size = batch_size
		multiplier = 1 if targeted else -1
		best_adv = inputs.clone()
		best_norm = (ep.ones(batch_size,1,1,1)*1e6).to(best_adv.device)
		adv = inputs.clone()
		_ , grad, (adversarial_loss,pred_labels) = loss_aux_and_grad(adv, labels,model)
		normalized_grad, grad_norm = self.normalize(grad)

		#Approximate the factor required to end Stage 1 within a ratio of the total steps
		ratio_factor =  self.calculate_factor(adversarial_loss, grad_norm, ratio=0.2)
		is_adv = ep.where(pred_labels!=labels,ep.ones_like(pred_labels),ep.zeros_like(pred_labels))
		already_adversarial = is_adv
		i=0
		ever_found = already_adversarial #help control stage 1 in case an image is already adversarial
		while i<self.steps:
			#Update Gamma
			gammas = self.gamma_step(i)

			#Stage 1: finding an adversarial sample quickly
			adv_stage_1 = adv + ratio_factor*multiplier*gammas*normalized_grad
			#adv_stage_1 = ep.clamp(adv_stage_1,0,255)

			#Stage 2
			perturbation = adv-inputs
			normalized_perturbation, perturbation_norm = self.normalize(perturbation)
			#Projection of the perturbation onto the gradient vector
			proj_perturbation = (normalized_perturbation*-normalized_grad).sum(1)

			#Case OUT
			# samples that are still adversarial  -> decrease distortion
			epsilons_out = gammas*perturbation_norm
			v_star = inputs + multiplier*proj_perturbation*normalized_grad
			v_adv_diff = (adv-v_star)
			diff_normed, diff_norm = self.normalize(v_adv_diff)
			distortion_control_out = epsilons_out**2-proj_perturbation**2
			distortion_control_out = ep.imum(ep.zeros(distortion_control_out.shape).to(best_adv.device), distortion_control_out)
			adv_stage_2_out = v_star + diff_normed*(distortion_control_out**0.5)


			#Case IN
			# samples that are not adversarial anymore -> increase distortion
			epsilons_in = perturbation_norm/gammas
			distortion_control_in = epsilons_in**2-perturbation_norm**2+proj_perturbation**2
			adv_stage_2_in = adv + multiplier*(proj_perturbation+distortion_control_in**0.5)*normalized_grad
			#Keep the right adversarial w.r.t. IN/OUT case
			adv_stage_2 = adv_stage_2_out*is_adv+adv_stage_2_in*~is_adv

			#If stage 1 has ever been succesful, keep stage 2. Keep stage 1 otherwise
			new_adv = adv_stage_1*(ever_found==0) + adv_stage_2*(ever_found!=0)
			#new_adv = ep.clamp(new_adv,0,255)
			#Update grads, current adversarial samples
			adv = new_adv
			pred_labels, grad,_ = self.classif_loss(model, adv, labels)
			normalized_grad, grad_norm = self.normalize(grad)
			is_adv = (pred_labels!=labels)
			ever_found = ever_found+is_adv.float()

			better_norm = perturbation_norm<=best_norm
			better_norm_and_adv = better_norm*is_adv
			best_adv = adv*better_norm_and_adv + best_adv*~better_norm_and_adv
			best_norm = p.norms.l2(flatten(best_adv), -1)

			i+=1


		best_adv = best_adv*(~already_adversarial)+inputs*(already_adversarial)

		return restore_type(best_adv.detach())