import boto3
import botocore
# import jsonschema
import json
import traceback
import zipfile
import os

from botocore.exceptions import ClientError

from extutil import remove_none_attributes, account_context, ExtensionHandler, ext, \
    current_epoch_time_usec_num, component_safe_name, lambda_env, random_id, \
    handle_common_errors

eh = ExtensionHandler()

# Import your clients
client = boto3.client('elbv2')

"""
eh calls
    eh.add_op() call MUST be made for the function to execute! Adds functions to the execution queue.
    eh.add_props() is used to add useful bits of information that can be used by this component or other components to integrate with this.
    eh.add_links() is used to add useful links to the console, the deployed infrastructure, the logs, etc that pertain to this component.
    eh.retry_error(a_unique_id_for_the_error(if you don't want it to fail out after 6 tries), progress=65, callback_sec=8)
        This is how to wait and try again
        Only set callback seconds for a wait, not an error
        @ext() runs the function if its operation is present and there isn't already a retry declared
    eh.add_log() is how logs are passed to the front-end
    eh.perm_error() is how you permanently fail the component deployment and send a useful message to the front-end
    eh.finish() just finishes the deployment and sends back message and progress
    *RARE*
    eh.add_state() takes a dictionary, merges existing with new
        This is specifically if CloudKommand doesn't need to store it for later. Thrown away at the end of the deployment.
        Preserved across retries, but not across deployments.
There are three elements of state preserved across retries:
    - eh.props
    - eh.links 
    - eh.state 
Wrap all operations you want to run with the following:
    @ext(handler=eh, op="your_operation_name")
Progress only needs to be explicitly reported on 1) a retry 2) an error. Finishing auto-sets progress to 100. 
"""

def lambda_handler(event, context):
    try:
        # All relevant data is generally in the event, excepting the region and account number
        print(f"event = {event}")
        region = account_context(context)['region']
        account_number = account_context(context)['number']

        # This copies the operations, props, links, retry data, and remaining operations that are sent from CloudKommand. 
        # Just always include this.
        eh.capture_event(event)

        # These are other important values you will almost always use
        prev_state = event.get("prev_state") or {}
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        cdef = event.get("component_def")
        cname = event.get("component_name")
        
        # Generate or read from component definition the identifier / name of the component here 
        name = eh.props.get("name") or cdef.get("name") or component_safe_name(project_code, repo_id, cname, no_underscores=True, no_uppercase=True, max_chars=32)

        # you pull in whatever arguments you care about
        """
        # Some examples. S3 doesn't really need any because each attribute is a separate call. 
        auto_verified_attributes = cdef.get("auto_verified_attributes") or ["email"]
        alias_attributes = cdef.get("alias_attributes") or ["preferred_username", "phone_number", "email"]
        username_attributes = cdef.get("username_attributes") or None
        """

        ### ATTRIBUTES THAT CAN BE SET ON INITIAL CREATION
        vpc_id = cdef.get('vpc_id')
        protocol = cdef.get("protocol") or 'HTTPS'
        protocol_version = cdef.get('protocol_version') or "HTTP1"
        port = cdef.get('port') or 443
        health_check_protocol = cdef.get('health_check_protocol') or 'HTTPS'
        health_check_port = cdef.get('health_check_port') or 'traffic-port'
        health_check_enabled = cdef.get('health_check_enabled') or True
        health_check_path = cdef.get('health_check_path') or '/'
        health_check_interval_seconds = cdef.get('health_check_interval_seconds') or 30
        health_check_timeout_seconds = cdef.get('health_check_timeout_seconds') or 5
        healthy_threshold_count = cdef.get('healthy_threshold_count') or 5
        unhealthy_threshold_count = cdef.get('unhealthy_threshold_count') or 2
        matcher = cdef.get('matcher') or {"HttpCode": '200,403'}
        target_type = cdef.get('target_type') or 'ip'
        tags = cdef.get('tags') or {} # this is converted to a [{"Key": key, "Value": value} , ...] format
        ip_address_type = cdef.get('tags') or 'ipv4'

        ### SPECIAL ATTRIBUTES THAT CAN ONLY BE ADDED POST INITIAL CREATION
        # supported by all load balancers
        deregistration_delay_timeout_seconds = cdef.get('deregistration_delay_timeout_seconds')
        stickiness_enabled = cdef.get("stickiness_enabled")
        stickiness_type = cdef.get("stickiness_type")
        # supported by Application Load Balancers and Network Load Balancers
        load_balancing_cross_zone_enabled = cdef.get("load_balancing_cross_zone_enabled")
        target_group_health_dns_failover_minimum_healthy_targets_count = cdef.get("target_group_health_dns_failover_minimum_healthy_targets_count")
        target_group_health_dns_failover_minimum_healthy_targets_percentage = cdef.get("target_group_health_dns_failover_minimum_healthy_targets_percentage")
        target_group_health_unhealthy_state_routing_minimum_healthy_targets_count = cdef.get("target_group_health_unhealthy_state_routing_minimum_healthy_targets_count")
        target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage = cdef.get("target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage")
        # supported only if the load balancer is an Application Load Balancer and the target is an instance or an IP address
        load_balancing_algorithm_type = cdef.get("load_balancing_algorithm_type")
        slow_start_duration_seconds = cdef.get("slow_start_duration_seconds")
        stickiness_app_cookie_cookie_name = cdef.get("stickiness_app_cookie_cookie_name")
        stickiness_app_cookie_duration_seconds = cdef.get("stickiness_app_cookie_duration_seconds")
        stickiness_lb_cookie_duration_seconds = cdef.get("stickiness_lb_cookie_duration_seconds")
        # supported only if the load balancer is an Application Load Balancer and the target is a Lambda function
        lambda_multi_value_headers_enabled = cdef.get("lambda_multi_value_headers_enabled")
        # supported only by Network Load Balancers
        deregistration_delay_connection_termination_enabled = cdef.get("deregistration_delay_connection_termination_enabled")
        preserve_client_ip_enabled = cdef.get("preserve_client_ip_enabled")
        proxy_protocol_v2_enabled = cdef.get("proxy_protocol_v2_enabled")
        # supported only by Gateway Load Balancers
        target_failover_on_deregistration = cdef.get("target_failover_on_deregistration")
        target_failover_on_unhealthy = cdef.get("target_failover_on_unhealthy")

        prev_state_def = prev_state.get("def", {})

        prev_deregistration_delay_timeout_seconds = prev_state_def.get('deregistration_delay_timeout_seconds')
        prev_stickiness_enabled = prev_state_def.get("stickiness_enabled")
        prev_stickiness_type = prev_state_def.get("stickiness_type")
        # supported by Application Load Balancers and Network Load Balancers
        prev_load_balancing_cross_zone_enabled = prev_state_def.get("load_balancing_cross_zone_enabled")
        prev_target_group_health_dns_failover_minimum_healthy_targets_count = prev_state_def.get("target_group_health_dns_failover_minimum_healthy_targets_count")
        prev_target_group_health_dns_failover_minimum_healthy_targets_percentage = prev_state_def.get("target_group_health_dns_failover_minimum_healthy_targets_percentage")
        prev_target_group_health_unhealthy_state_routing_minimum_healthy_targets_count = prev_state_def.get("target_group_health_unhealthy_state_routing_minimum_healthy_targets_count")
        prev_target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage = prev_state_def.get("target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage")
        # supported only if the load balancer is an Application Load Balancer and the target is an instance or an IP address
        prev_load_balancing_algorithm_type = prev_state_def.get("load_balancing_algorithm_type")
        prev_slow_start_duration_seconds = prev_state_def.get("slow_start_duration_seconds")
        prev_stickiness_app_cookie_cookie_name = prev_state_def.get("stickiness_app_cookie_cookie_name")
        prev_stickiness_app_cookie_duration_seconds = prev_state_def.get("stickiness_app_cookie_duration_seconds")
        prev_stickiness_lb_cookie_duration_seconds = prev_state_def.get("stickiness_lb_cookie_duration_seconds")
        # supported only if the load balancer is an Application Load Balancer and the target is a Lambda function
        prev_lambda_multi_value_headers_enabled = prev_state_def.get("lambda_multi_value_headers_enabled")
        # supported only by Network Load Balancers
        prev_deregistration_delay_connection_termination_enabled = prev_state_def.get("deregistration_delay_connection_termination_enabled")
        prev_preserve_client_ip_enabled = prev_state_def.get("preserve_client_ip_enabled")
        prev_proxy_protocol_v2_enabled = prev_state_def.get("proxy_protocol_v2_enabled")
        # supported only by Gateway Load Balancers
        prev_target_failover_on_deregistration = prev_state_def.get("target_failover_on_deregistration")
        prev_target_failover_on_unhealthy = prev_state_def.get("target_failover_on_unhealthy")



        # remove any None values from the attributes dictionary
        attributes = remove_none_attributes({
            "Name": name,
            "Protocol": protocol,
            "ProtocolVersion": protocol_version,
            "Port": port,
            "VpcId": vpc_id,
            "HealthCheckProtocol": health_check_protocol,
            "HealthCheckPort": health_check_port,
            "HealthCheckEnabled": health_check_enabled,
            "HealthCheckPath": health_check_path,
            "HealthCheckIntervalSeconds": health_check_interval_seconds,
            "HealthCheckTimeoutSeconds": health_check_timeout_seconds,
            "HealthyThresholdCount": healthy_threshold_count,
            "UnhealthyThresholdCount": unhealthy_threshold_count,
            "Matcher": matcher,
            "TargetType": target_type,
            "Tags": [{"Key": f"{key}", "Value": f"{value}"} for key, value in tags.items()],
            "IpAddressType": ip_address_type
        })

        special_attributes = remove_none_attributes({
            "deregistration_delay.timeout_seconds": deregistration_delay_timeout_seconds,
            "stickiness.enabled": stickiness_enabled,
            "stickiness.type": stickiness_type,
            "load_balancing.cross_zone.enabled": load_balancing_cross_zone_enabled,
            "target_group_health.dns_failover.minimum_healthy_targets.count": target_group_health_dns_failover_minimum_healthy_targets_count,
            "target_group_health.dns_failover.minimum_healthy_targets.percentage": target_group_health_dns_failover_minimum_healthy_targets_percentage,
            "target_group_health.unhealthy_state_routing.minimum_healthy_targets.count": target_group_health_unhealthy_state_routing_minimum_healthy_targets_count,
            "target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage": target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage,
            "load_balancing.algorithm.type": load_balancing_algorithm_type,
            "slow_start.duration_seconds": slow_start_duration_seconds,
            "stickiness.app_cookie.cookie_name": stickiness_app_cookie_cookie_name,
            "stickiness.app_cookie.duration_seconds": stickiness_app_cookie_duration_seconds,
            "stickiness.lb_cookie.duration_seconds": stickiness_lb_cookie_duration_seconds,
            "lambda.multi_value_headers.enabled": lambda_multi_value_headers_enabled,
            "deregistration_delay.connection_termination.enabled": deregistration_delay_connection_termination_enabled,
            "preserve_client_ip.enabled": preserve_client_ip_enabled,
            "proxy_protocol_v2.enabled": proxy_protocol_v2_enabled,
            "target_failover.on_deregistration": target_failover_on_deregistration,
            "target_failover.on_unhealthy": target_failover_on_unhealthy
        })

        prev_state_special_attributes = remove_none_attributes({
            "deregistration_delay.timeout_seconds": deregistration_delay_timeout_seconds,
            "stickiness.enabled": stickiness_enabled,
            "stickiness.type": stickiness_type,
            "load_balancing.cross_zone.enabled": load_balancing_cross_zone_enabled,
            "target_group_health.dns_failover.minimum_healthy_targets.count": target_group_health_dns_failover_minimum_healthy_targets_count,
            "target_group_health.dns_failover.minimum_healthy_targets.percentage": target_group_health_dns_failover_minimum_healthy_targets_percentage,
            "target_group_health.unhealthy_state_routing.minimum_healthy_targets.count": target_group_health_unhealthy_state_routing_minimum_healthy_targets_count,
            "target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage": target_group_health_unhealthy_state_routing_minimum_healthy_targets_percentage,
            "load_balancing.algorithm.type": load_balancing_algorithm_type,
            "slow_start.duration_seconds": slow_start_duration_seconds,
            "stickiness.app_cookie.cookie_name": stickiness_app_cookie_cookie_name,
            "stickiness.app_cookie.duration_seconds": stickiness_app_cookie_duration_seconds,
            "stickiness.lb_cookie.duration_seconds": stickiness_lb_cookie_duration_seconds,
            "lambda.multi_value_headers.enabled": lambda_multi_value_headers_enabled,
            "deregistration_delay.connection_termination.enabled": deregistration_delay_connection_termination_enabled,
            "preserve_client_ip.enabled": preserve_client_ip_enabled,
            "proxy_protocol_v2.enabled": proxy_protocol_v2_enabled,
            "target_failover.on_deregistration": target_failover_on_deregistration,
            "target_failover.on_unhealthy": target_failover_on_unhealthy
        })


        ### DECLARE STARTING POINT
        pass_back_data = event.get("pass_back_data", {}) # pass_back_data only exists if this is a RETRY
        # If a RETRY, then don't set starting point
        if pass_back_data:
            pass # If pass_back_data exists, then eh has already loaded in all relevant RETRY information.
        # If NOT retrying, and we are instead upserting, then we start with the GET STATE call
        elif event.get("op") == "upsert":

            old_name = None
            old_protocol = None
            old_protocol_version = None
            old_port = None
            old_vpc_id = None
            old_target_type = None
            old_ip_address_type = None

            try:
                old_name = prev_state["props"]["name"]
                old_protocol = prev_state["props"]["protocol"]
                old_protocol_version = prev_state["props"]["protocol_version"]
                old_port = prev_state["props"]["port"]
                old_vpc_id = prev_state["props"]["vpc_id"]
                old_target_type = prev_state["props"]["target_type"]
                old_ip_address_type = prev_state["props"]["ip_address_type"]
            except:
                pass

            eh.add_op("get_target_group")

            # If any non-editable fields have changed, we are choosing to fail. 
            # We are NOT choosing to delete and recreate because a listener may be attached and that MUST be removed before the target group can be deleted. 
            # Therefore a switchover is necessary to change un-editable values.
            if (old_name and old_name != name) or \
                (old_protocol and old_protocol != protocol) or \
                (old_protocol_version and old_protocol_version != protocol_version) or \
                (old_port and old_port != port) or \
                (old_vpc_id and old_vpc_id != vpc_id) or \
                (old_target_type and old_target_type != target_type) or \
                (old_ip_address_type and old_ip_address_type != ip_address_type):

                eh.add_log("You may not edit the name, protocol, protocol_version, port, vpc_id, target_type, or ip_address_type on an existing target_group. Please create a new component and associate the listener to your updated target group to get the desired configuration.", {"error": str(e)}, is_error=True)
                eh.perm_error(str(e), 20)

        # If NOT retrying, and we are instead deleting, then we start with the DELETE call 
        #   (sometimes you start with GET STATE if you need to make a call for the identifier)
        elif event.get("op") == "delete":
            eh.add_op("delete_target_group")

        # The ordering of call declarations should generally be in the following order
        # GET STATE
        # CREATE
        # UPDATE
        # DELETE
        # GENERATE PROPS
        
        ### The section below DECLARES all of the calls that can be made. 
        ### The eh.add_op() function MUST be called for actual execution of any of the functions. 

        ### GET STATE
        get_target_group(name, attributes, region, prev_state)

        ### DELETE CALL(S)
        delete_target_group(name)

        ### CREATE CALL(S) (occasionally multiple)
        create_target_group(attributes, region)
        
        ### UPDATE CALLS (common to have multiple)
        # You want ONE function per boto3 update call, so that retries come back to the EXACT same spot. 
        remove_tags(name)
        set_tags()
        update_target_group(attributes)
        update_target_group_special_attributes(special_attributes, prev_state)

        ### GENERATE PROPS (sometimes can be done in get/create)

        # IMPORTANT! ALWAYS include this. Sends back appropriate data to CloudKommand.
        return eh.finish()

    # You want this. Leave it.
    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

### GET STATE
# ALWAYS put the ext decorator on ALL calls that are referenced above
# This is ONLY called when this operation is slated to occur.
# GENERALLY, this function will make a bunch of eh.add_op() calls which determine what actions will be executed.
#   The eh.add_op() call MUST be made for the function to execute!
# eh.add_props() is used to add useful bits of information that can be used by this component or other components to integrate with this.
# eh.add_links() is used to add useful links to the console, the deployed infrastructure, the logs, etc that pertain to this component.
@ext(handler=eh, op="get_target_group")
def get_target_group(name, attributes, region, prev_state):
    client = boto3.client("elbv2")

    if prev_state and prev_state.get("props") and prev_state.get("props").get("name"):
        prev_name = prev_state.get("props").get("name")
        if name != prev_name:
            eh.perm_error("Cannot Change Target Group Name", progress=0)
            return None
    
    # Try to get the target group. If you succeed, record the props and links from the current target group
    try:
        response = client.describe_target_groups(Names=[name])
        target_group_to_use = None
        target_group_arn = None
        if response and response.get("TargetGroups") and len(response.get("TargetGroups")) > 0:
            target_group_to_use = response.get("TargetGroups")[0]
            target_group_arn = target_group_to_use.get("TargetGroupArn")
            eh.add_state({"target_group_arn": target_group_to_use.get("TargetGroupArn"), "region": region})
            eh.add_props({
                "name": name,
                "arn": target_group_to_use.get("TargetGroupArn"),
                "vpc_id": target_group_to_use.get("VpcId"),
                "port": target_group_to_use.get("Port"),
                "load_balancer_arns": target_group_to_use.get("LoadBalancerArns"),
                "protocol": target_group_to_use.get("Protocol"),
                "protocol_version": target_group_to_use.get("ProtocolVersion"),
                "target_type": target_group_to_use.get("TargetType"),
                "ip_address_type": target_group_to_use.get("IpAddressType")
            })
            eh.add_links({"Target Group": gen_target_group_link(region, target_group_arn)})
        # else: # If there is no target group and there is no exception, handle it hereb
        #     eh.add_log("Target Group Does Not Exist", {"name": name})
        #     eh.add_op("create_target_group")
    # If there is no target group and there is an exception handle it here
    except client.exceptions.TargetGroupNotFoundException:
        eh.add_log("Target Group Does Not Exist", {"name": name})
        eh.add_op("create_target_group")
        return 0
    except ClientError as e:
        print(str(e))
        eh.add_log("Get Target Group Error", {"error": str(e)}, is_error=True)
        eh.retry_error("Get Target Group Error", 30)
        return 0

    # If the target_group exists, then setup any followup tasks
    # If there are tags specified, figure out which ones need to be added and which ones need to be removed
    if attributes.get("tags"):
        try:
            # Try to get the current tags
            response = client.describe_tags(ResourceArns=[target_group_arn])
            relevant_items = [item for item in response.get("TagDescriptions") if item.get("ResourceArn") == target_group_arn]
            current_tags = {}
            # Parse out the current tags
            if len(relevant_items) > 0:
                relevant_item = relevant_items[0]
                if relevant_item.get("Tags"):
                    current_tags = {item.get("Key") : item.get("Value") for item in relevant_item.get("Tags")}
            tags = attributes.get("tags")
            # Compare the current tags to the desired tags
            if tags != current_tags:
                remove_tags = [k for k in current_tags.keys() if k not in tags]
                add_tags = {k:v for k,v in tags.items() if v != current_tags.get(k)}
                if remove_tags:
                    eh.add_op("remove_tags", remove_tags)
                if add_tags:
                    eh.add_op("set_tags", add_tags)
        # If the target group does not exist, some wrong has happened. Probably don't permanently fail though, try to continue.
        except client.exceptions.TargetGroupNotFoundException:
            eh.add_log("Target Group Not Found", {"arn": target_group_arn})
            pass
    # If there are no tags specified, make sure to remove any straggler tags
    else:
        eh.add_op("remove_all_tags")

            
@ext(handler=eh, op="create_target_group")
def create_target_group(attributes, region):

    try:
        response = client.create_target_group(**attributes)

        eh.add_log("Created Target Group", response)
        eh.add_state({"target_group_arn": response.get("TargetGroupArn")})
        eh.add_props({
            "name": attributes.get("name"),
            "arn": response.get("TargetGroupArn"),
            "vpc_id": response.get("VpcId"),
            "port": response.get("Port"),
            "load_balancer_arns": response.get("LoadBalancerArns"),
            "protocol": response.get("Protocol"),
            "protocol_version": response.get("ProtocolVersion"),
            "target_type": response.get("TargetType"),
            "ip_address_type": response.get("IpAddressType")
        })

        eh.add_links({"Target Group": gen_target_group_link(region, response.get("TargetGroupArn"))})

    except client.exceptions.DuplicateTargetGroupNameException:
        eh.add_log(f"Target Group name {attributes.get('name')} already exists", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTargetGroupsException:
        eh.add_log(f"AWS Quota for Target Groups reached. Please increase your quota and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.InvalidConfigurationRequestException:
        eh.add_log("Invalid Target Group Parameters", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)
    except client.exceptions.TooManyTagsException:
        eh.add_log("Too Many Tags on Target Group. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 20)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Creating Target Group", progress=20)

@ext(handler=eh, op="remove_tags")
def remove_tags():

    remove_tags = eh.ops['remove_tags']
    target_group_arn = eh.state["target_group_arn"]

    try:
        response = client.remove_tags(
            ResourceArns=[target_group_arn],
            TagKeys=[remove_tags]
        )
    except client.exceptions.TargetGroupNotFoundException:
        eh.add_log("Target Group Not Found", {"arn": target_group_arn})

    except ClientError as e:
        handle_common_errors(e, eh, "Error Removing Target Group Tags", progress=80)


@ext(handler=eh, op="set_tags")
def set_tags():

    tags = eh.ops.get("set_tags")
    target_group_arn = eh.state["target_group_arn"]
    try:
        response = client.add_tags(
            ResourceArns=[target_group_arn],
            Tags=[{"Key": key, "Value": value} for key, value in tags.items()]
        )
        eh.add_log("Tags Added", response)

    except client.exceptions.TargetGroupNotFoundException:
        eh.add_log("Target Group Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except client.exceptions.DuplicateTagKeysException:
        eh.add_log(f"Duplicate Tags Found. Please remove duplicates and try again.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except client.exceptions.TooManyTagsException:
        eh.add_log(f"Too Many Tags on Target Group. You may have 50 tags per resource.", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Adding Tags", progress=90)

@ext(handler=eh, op="update_target_group")
def update_target_group(attributes):
    attributes_to_remove = ["Name", "Protocol", "ProtocolVersion", "Port", "VpcId", "TargetType", "Tags", "IpAddressType"]
    filtered_attributes = [attr for attr in attributes if attr in attributes_to_remove]
    
    region = eh.state["region"]

    try:
        response = client.modify_target_group(**filtered_attributes)

        eh.add_log("Modified Target Group", response)

        if response and response.get("TargetGroups") and len(response.get("TargetGroups")) > 0:
            relevant_target_group = response.get("TargetGroups")[0]
            eh.add_state({"target_group_arn": relevant_target_group.get("TargetGroupArn")})
            eh.add_props({
                "name": attributes.get("name"),
                "arn": relevant_target_group.get("TargetGroupArn"),
                "vpc_id": relevant_target_group.get("VpcId"),
                "port": relevant_target_group.get("Port"),
                "load_balancer_arns": relevant_target_group.get("LoadBalancerArns"),
                "protocol": relevant_target_group.get("Protocol"),
                "protocol_version": relevant_target_group.get("ProtocolVersion"),
                "target_type": relevant_target_group.get("TargetType"),
                "ip_address_type": relevant_target_group.get("IpAddressType")
            })
            eh.add_links({"Target Group": gen_target_group_link(region, response.get("TargetGroupArn"))})
            
    except client.exceptions.TargetGroupNotFoundException:
        eh.add_log("Target Group Not Found", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)
    except client.exceptions.InvalidConfigurationRequestException:
        eh.add_log("Invalid Target Group Parameters", {"error": str(e)}, is_error=True)
        eh.perm_error(str(e), 80)

    except ClientError as e:
        handle_common_errors(e, eh, "Error Adding Tags", progress=90)
    

@ext(handler=eh, op="update_target_group_special_attributes")
def update_target_group_special_attributes(special_attributes, prev_special_attributes):

    target_group_arn = eh.state["target_group_arn"]

    if tags != current_tags:
        remove_tags = [k for k in current_tags.keys() if k not in tags]
        add_tags = {k:v for k,v in tags.items() if v != current_tags.get(k)}
        if remove_tags:
            eh.add_op("remove_tags", remove_tags)
        if add_tags:
            eh.add_op("set_tags", add_tags)




    
    try:
        response = client.modify_target_group_attributes(
            TargetGroupArn=target_group_arn,
            Attributes=[
                {
                    'Key': 'string',
                    'Value': 'string'
                },
            ]
        )


@ext(handler=eh, op="delete_target_group")
def delete_target_group(name):
    pass



def gen_target_group_link(region, target_group_arn):
    return f"https://{region}.console.aws.amazon.com/ec2/home?region={region}#TargetGroup:targetGroupArn={target_group_arn}"




"""

NOTES:

TARGET_GROUP_ATTRIBUTES_THAT_CAN_BE_SET_WITH_IP

deregistration_delay.timeout_seconds
The amount of time for Elastic Load Balancing to wait before deregistering a target. The range is 0–3600 seconds. The default value is 300 seconds.

load_balancing.algorithm.type
The load balancing algorithm determines how the load balancer selects targets when routing requests. The value is round_robin or least_outstanding_requests. The default is round_robin.

load_balancing.cross_zone.enabled
Indicates whether cross zone load balancing is enabled. The value is true, false or use_load_balancer_configuration. The default is use_load_balancer_configuration.

slow_start.duration_seconds
The time period, in seconds, during which the load balancer sends a newly registered target a linearly increasing share of the traffic to the target group. The range is 30–900 seconds (15 minutes). The default is 0 seconds (disabled).

stickiness.enabled
Indicates whether sticky sessions are enabled. The value is true or false. The default is false.

stickiness.app_cookie.cookie_name
The name of the application cookie. The application cookie name cannot have the following prefixes: AWSALB, AWSALBAPP, or AWSALBTG; they're reserved for use by the load balancer.

stickiness.app_cookie.duration_seconds
The application-based cookie expiration period, in seconds. After this period, the cookie is considered stale. The minimum value is 1 second and the maximum value is 7 days (604800 seconds). The default value is 1 day (86400 seconds).

stickiness.lb_cookie.duration_seconds
The duration-based cookie expiration period, in seconds. After this period, the cookie is considered stale. The minimum value is 1 second and the maximum value is 7 days (604800 seconds). The default value is 1 day (86400 seconds).

stickiness.type
The type of stickiness. The possible values are lb_cookie and app_cookie.

target_group_health.dns_failover.minimum_healthy_targets.count
The minimum number of targets that must be healthy. If the number of healthy targets is below this value, mark the zone as unhealthy in DNS, so that traffic is routed only to healthy zones. The possible values are off or an integer from 1 to the maximum number of targets. The default is off.

target_group_health.dns_failover.minimum_healthy_targets.percentage
The minimum percentage of targets that must be healthy. If the percentage of healthy targets is below this value, mark the zone as unhealthy in DNS, so that traffic is routed only to healthy zones. The possible values are off or an integer from 1 to 100. The default is off.

target_group_health.unhealthy_state_routing.minimum_healthy_targets.count
The minimum number of targets that must be healthy. If the number of healthy targets is below this value, send traffic to all targets, including unhealthy targets. The range is 1 to the maximum number of targets. The default is 1.

target_group_health.unhealthy_state_routing.minimum_healthy_targets.percentage
The minimum percentage of targets that must be healthy. If the percentage of healthy targets is below this value, send traffic to all targets, including unhealthy targets. The possible values are off or an integer from 1 to 100. The default is off.


"""